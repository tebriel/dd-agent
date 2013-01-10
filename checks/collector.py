# Core modules
import os
import re
import logging
import subprocess
import sys
import time
import datetime
import socket

import modules

from util import getOS, get_uuid, md5, Timer
from config import get_version
from checks import gethostname

import checks.system.unix as u
import checks.system.win32 as w32
from checks.nagios import Nagios
from checks.build import Hudson
from checks.db.mysql import MySql
from checks.db.mongo import MongoDb
from checks.db.couch import CouchDb
from checks.db.mcache import Memcache
from checks.queue import RabbitMq
from checks.ganglia import Ganglia
from checks.cassandra import Cassandra
from checks.datadog import Dogstreams, DdForwarder
from checks.db.elastic import ElasticSearch, ElasticSearchClusterStatus
from checks.wmi_check import WMICheck
from checks.ec2 import EC2
from checks.check_status import CheckStatus, CollectorStatus, EmitterStatus
from resources.processes import Processes as ResProcesses


logger = logging.getLogger('collector')
checks_logger = logging.getLogger('checks')


class Collector(object):
    """
    The collector is responsible for collecting data from each check and
    passing it along to the emitters, who send it to their final destination.
    """

    def __init__(self, agentConfig, emitters, systemStats):
        self.agentConfig = agentConfig
        # system stats is generated by config.get_system_stats
        self.agentConfig['system_stats'] = systemStats
        # agent config is used during checks, system_stats can be accessed through the config
        self.os = getOS()
        self.plugins = None
        self.emitters = emitters            
        self.metadata_interval = int(agentConfig.get('metadata_interval', 10 * 60))
        self.metadata_start = time.time()
        socket.setdefaulttimeout(15)
        self.run_count = 0
        self.continue_running = True
        self.metadata_cache = None
        
        # Unix System Checks
        self._unix_system_checks = {
            'disk': u.Disk(checks_logger),
            'io': u.IO(checks_logger),
            'load': u.Load(checks_logger),
            'memory': u.Memory(checks_logger),
            'network': u.Network(checks_logger),
            'processes': u.Processes(checks_logger),
            'cpu': u.Cpu(checks_logger)
        }

        # Win32 System `Checks
        self._win32_system_checks = {
            'disk': w32.Disk(checks_logger),
            'io': w32.IO(checks_logger),
            'proc': w32.Processes(checks_logger),
            'memory': w32.Memory(checks_logger),
            'network': w32.Network(checks_logger),
            'cpu': w32.Cpu(checks_logger)
        }

        # Old-style metric checks
        self._couchdb = CouchDb(checks_logger)
        self._mongodb = MongoDb(checks_logger)
        self._mysql = MySql(checks_logger)
        self._rabbitmq = RabbitMq()
        self._ganglia = Ganglia(checks_logger)
        self._cassandra = Cassandra()
        self._dogstream = Dogstreams.init(checks_logger, self.agentConfig)
        self._ddforwarder = DdForwarder(checks_logger, self.agentConfig)
        self._ec2 = EC2(checks_logger)

        # Metric Checks
        self._metrics_checks = [
            ElasticSearch(checks_logger),
            WMICheck(checks_logger),
            Memcache(checks_logger),
        ]

        # Custom metric checks
        for module_spec in [s.strip() for s in self.agentConfig.get('custom_checks', '').split(',')]:
            if len(module_spec) == 0: continue
            try:
                self._metrics_checks.append(modules.load(module_spec, 'Check')(checks_logger))
                logger.info("Registered custom check %s" % module_spec)
            except Exception, e:
                logger.exception('Unable to load custom check module %s' % module_spec)

        # Event Checks
        self._event_checks = [
            ElasticSearchClusterStatus(checks_logger),
            Nagios(socket.gethostname()),
            Hudson()
        ]

        # Resource Checks
        self._resources_checks = [
            ResProcesses(checks_logger,self.agentConfig)
        ]

    def stop(self):
        """
        Tell the collector to stop at the next logical point.
        """
        # This is called when the process is being killed, so 
        # try to stop the collector as soon as possible.
        # Most importantly, don't try to submit to the emitters
        # because the forwarder is quite possibly already killed
        # in which case we'll get a misleading error in the logs.
        # Best to not even try.
        self.continue_running = False
    
    def run(self, checksd=None):
        """
        Collect data from each check and submit their data.
        """
        timer = Timer()
        self.run_count += 1
        logger.debug("Starting collection run #%s" % self.run_count)

        payload = self._build_payload()
        metrics = payload['metrics']
        events = payload['events']

        # Run the system checks. Checks will depend on the OS
        if self.os == 'windows':
            # Win32 system checks
            metrics.extend(self._win32_system_checks['disk'].check(self.agentConfig))
            metrics.extend(self._win32_system_checks['memory'].check(self.agentConfig))
            metrics.extend(self._win32_system_checks['cpu'].check(self.agentConfig))
            metrics.extend(self._win32_system_checks['network'].check(self.agentConfig))
            metrics.extend(self._win32_system_checks['io'].check(self.agentConfig))
            metrics.extend(self._win32_system_checks['proc'].check(self.agentConfig))
        else:
            # Unix system checks
            sys_checks = self._unix_system_checks

            diskUsage = sys_checks['disk'].check(self.agentConfig)
            if diskUsage and len(diskUsage) == 2:
                payload["diskUsage"] = diskUsage[0]
                payload["inodes"] = diskUsage[1]

            load = sys_checks['load'].check(self.agentConfig)
            payload.update(load)
                
            memory = sys_checks['memory'].check(self.agentConfig)
            if memory:
                payload.update({
                    'memPhysUsed' : memory.get('physUsed'), 
                    'memPhysPctUsable' : memory.get('physPctUsable'), 
                    'memPhysFree' : memory.get('physFree'), 
                    'memPhysTotal' : memory.get('physTotal'), 
                    'memPhysUsable' : memory.get('physUsable'), 
                    'memSwapUsed' : memory.get('swapUsed'), 
                    'memSwapFree' : memory.get('swapFree'), 
                    'memSwapPctFree' : memory.get('swapPctFree'), 
                    'memSwapTotal' : memory.get('swapTotal'), 
                    'memCached' : memory.get('physCached'), 
                    'memBuffers': memory.get('physBuffers'),
                    'memShared': memory.get('physShared')
                })

            ioStats = sys_checks['io'].check(self.agentConfig)
            if ioStats:
                payload['ioStats'] = ioStats

            processes = sys_checks['processes'].check(self.agentConfig)
            payload.update({'processes': processes})

            networkTraffic = sys_checks['network'].check(self.agentConfig)
            payload.update({'networkTraffic': networkTraffic})

            cpuStats = sys_checks['cpu'].check(self.agentConfig)
            if cpuStats:
                payload.update(cpuStats)

        # Run old-style checks
        mysqlStatus = self._mysql.check(self.agentConfig)
        rabbitmq = self._rabbitmq.check(checks_logger, self.agentConfig)
        mongodb = self._mongodb.check(self.agentConfig)
        couchdb = self._couchdb.check(self.agentConfig)
        gangliaData = self._ganglia.check(self.agentConfig)
        cassandraData = self._cassandra.check(checks_logger, self.agentConfig)
        dogstreamData = self._dogstream.check(self.agentConfig)
        ddforwarderData = self._ddforwarder.check(self.agentConfig)

        if gangliaData is not False and gangliaData is not None:
            payload['ganglia'] = gangliaData
           
        if cassandraData is not False and cassandraData is not None:
            payload['cassandra'] = cassandraData
            
        # MySQL Status
        if mysqlStatus:
            payload.update(mysqlStatus)
       
        # RabbitMQ
        if rabbitmq:
            payload['rabbitMQ'] = rabbitmq
        
        # MongoDB
        if mongodb:
            if mongodb.has_key('events'):
                events['Mongo'] = mongodb['events']['Mongo']
                del mongodb['events']
            payload['mongoDB'] = mongodb
            
        # CouchDB
        if couchdb:
            payload['couchDB'] = couchdb
        
        # dogstream
        if dogstreamData:
            dogstreamEvents = dogstreamData.get('dogstreamEvents', None)
            if dogstreamEvents:
                if 'dogstream' in payload['events']:
                    events['dogstream'].extend(dogstreamEvents)
                else:
                    events['dogstream'] = dogstreamEvents
                del dogstreamData['dogstreamEvents']

            payload.update(dogstreamData)

        # metrics about the forwarder
        if ddforwarderData:
            payload['datadog'] = ddforwarderData
 
        # Process the event checks. 
        for event_check in self._event_checks:
            event_data = event_check.check(checks_logger, self.agentConfig)
            if event_data:
                events[event_check.key] = event_data

        # Resources checks
        if self.os != 'windows':
            has_resource = False
            for resources_check in self._resources_checks:
                resources_check.check()
                snaps = resources_check.pop_snapshots()
                if snaps:
                    has_resource = True
                    res_value = { 'snaps': snaps,
                                  'format_version': resources_check.get_format_version() }                              
                    res_format = resources_check.describe_format_if_needed()
                    if res_format is not None:
                        res_value['format_description'] = res_format
                    payload['resources'][resources_check.RESOURCE_KEY] = res_value
     
            if has_resource:
                payload['resources']['meta'] = {
                            'api_key': self.agentConfig['api_key'],
                            'host': payload['internalHostname'],
                        }

        # newer-style checks (not checks.d style)
        for metrics_check in self._metrics_checks:
            res = metrics_check.check(self.agentConfig)
            if res:
                metrics.extend(res)

        # checks.d checks
        check_statuses = []
        checksd = checksd or []
        for check in checksd:
            if not self.continue_running:
                return
            logger.debug("Running check %s" % check.name)
            instance_statuses = [] 
            metric_count = 0
            event_count = 0
            try:
                # Run the check.
                instance_statuses = check.run()

                # Collect the metrics and events.
                current_check_metrics = check.get_metrics()
                current_check_events = check.get_events()

                # Save them for the payload.
                metrics.extend(current_check_metrics)
                if current_check_events:
                    if check.name not in events:
                        events[check.name] = current_check_events
                    else:
                        events[check.name] += current_check_events

                # Save the status of the check.
                metric_count = len(current_check_metrics)
                event_count = len(current_check_events)
            except Exception, e:
                logger.exception("Error running check %s" % check.name)
            check_status = CheckStatus(check.name, instance_statuses, metric_count, event_count)
            check_statuses.append(check_status)

        # Store the metrics and events in the payload.
        payload['metrics'] = metrics
        payload['events'] = events
        collect_duration = timer.step()

        emitter_statuses = self._emit(payload)
        emit_duration = timer.step()

        # Persist the status of the collection run.
        try:
            CollectorStatus(check_statuses, emitter_statuses, self.metadata_cache).persist()
        except Exception:
            logger.exception("Error persisting collector status")

        logger.info("Finished run #%s. Collection time: %ss. Emit time: %ss" %
                    (self.run_count, round(collect_duration, 2), round(emit_duration, 2)))

    def _emit(self, payload):
        """ Send the payload via the emitters. """
        statuses = []
        for emitter in self.emitters:
            # Don't try to send to an emitter if we're stopping/
            if not self.continue_running:
                return statuses
            name = emitter.__name__
            emitter_status = EmitterStatus(name)
            try:
                emitter(payload, checks_logger, self.agentConfig)
            except Exception, e:
                logger.exception("Error running emitter: %s" % emitter.__name__)
                emitter_status = EmitterStatus(name, e)
            statuses.append(emitter_status)
        return statuses

    def _is_first_run(self):
        return self.run_count <= 1

    def _build_payload(self):
        """
        Return an dictionary that contains all of the generic payload data.
        """

        payload = {
            'collection_timestamp': time.time(),
            'os' : self.os,
            'python': sys.version,
            'agentVersion' : self.agentConfig['version'],
            'apiKey': self.agentConfig['api_key'],
            'events': {},
            'metrics': [],
            'resources': {},
            'internalHostname' : gethostname(self.agentConfig),
            'uuid' : get_uuid(),
        }

        # Include system stats on first postback
        if self._is_first_run():
            payload['systemStats'] = self.agentConfig.get('system_stats', {})
            # Also post an event in the newsfeed
            payload['events']['System'] = [{'api_key': self.agentConfig['api_key'],
                                 'host': payload['internalHostname'],
                                 'timestamp': int(time.mktime(datetime.datetime.now().timetuple())),
                                 'event_type':'Agent Startup',
                                 'msg_text': 'Version %s' % get_version()
                                 }]

        # Periodically send the host metadata.
        if self._is_first_run() or self._should_send_metadata():
            payload['meta'] = self._get_metadata()
            self.metadata_cache = payload['meta']
            # Add static tags from the configuration file
            if self.agentConfig['tags'] is not None:
                payload['tags'] = self.agentConfig['tags']

        return payload

    def _get_metadata(self):
        metadata = self._ec2.get_metadata()
        if metadata.get('hostname'):
            metadata['ec2-hostname'] = metadata.get('hostname')

        # if hostname is set in the configuration file
        # use that instead of gethostname
        # gethostname is vulnerable to 2 hosts: x.domain1, x.domain2
        # will cause both to be aliased (see #157)
        if self.agentConfig.get('hostname'):
            metadata['agent-hostname'] = self.agentConfig.get('hostname')
            metadata['hostname'] = metadata['agent-hostname']
        else:
            try:
                metadata["hostname"] = socket.gethostname()
            except:
                pass
        try:
            metadata["fqdn"] = socket.getfqdn()
        except:
            pass

        return metadata

    def _should_send_metadata(self):
        # If the interval has passed, send the metadata again
        now = time.time()
        if now - self.metadata_start >= self.metadata_interval:
            checks_logger.debug('Metadata interval has passed. Sending metadata.')
            self.metadata_start = now
            return True

        return False


