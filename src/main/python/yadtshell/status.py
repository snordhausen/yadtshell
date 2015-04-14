# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
#   YADT - an Augmented Deployment Tool
#   Copyright (C) 2010-2014  Immobilien Scout GmbH
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import (absolute_import, print_function)

import glob
import os
import logging
import sys
import inspect
import shlex
import yaml
import simplejson as json
import re

from twisted.internet import (defer, protocol, reactor)
from twisted.internet.defer import succeed
from twisted.python.failure import Failure


from hostexpand.HostExpander import HostExpander
import yadtshell
from yadtshell.rest_simple import rest_call
from yadtshell.util import compute_dependency_scores, filter_missing_services


logger = logging.getLogger('status')

try:
    from yaml import CLoader as yaml_loader
    logger.debug("using C implementation of yaml")
except ImportError:
    from yaml import Loader as yaml_loader
    logger.debug("using default yaml")
try:
    import cPickle as pickle
    logger.debug("using C implementation of pickle")
except ImportError:
    import pickle
    logger.debug("using default pickle")

local_service_collector = None


def status_cb(protocol=None):
    return status()


def handle_ignored_status(result_or_failure, component_name, components, pi):
    if isinstance(result_or_failure, Failure):
        ignored = False
    else:
        logger.debug("ignored message for %s: %s" % (component_name, result_or_failure))
        ignored = True

    if ignored:
        ignored_host = yadtshell.components.IgnoredHost(component_name, result_or_failure)
        components[ignored_host.uri] = ignored_host
        return succeed(ignored_host)
    else:
        p = yadtshell.twisted.YadtProcessProtocol(
            component_name, '/usr/bin/yadt-status', pi, out_log_level=logging.NOTSET)
        p.deferred.name = component_name
        cmd = shlex.split(yadtshell.settings.SSH) + [component_name]
        reactor.spawnProcess(p, cmd[0], cmd, os.environ)
        return p.deferred


def query_status(component_name, components, pi=None):
    short_hostname = re.sub("\\..*", "", component_name)

    d = rest_call("http://%s:%s/api/v1/hosts/%s/status-ignored" % (
        yadtshell.settings.ybc.host,
        yadtshell.settings.ybc.port,
        short_hostname))

    d.addCallbacks(callback=handle_ignored_status, callbackArgs=[component_name, components, pi],
                   errback=handle_ignored_status, errbackArgs=[component_name, components, pi])
    return d


def handle_failing_status(failure, components, ignore_unreachable_hosts=False):
    if failure.value.exitCode == 127:
        logger.critical('No yadt-minion installed on remote host %s',
                        failure.value.component)
    if failure.value.exitCode == 255:
        if yadtshell.settings.ignore_unreachable_hosts or ignore_unreachable_hosts:
            logger.warning('Cannot reach host %s; temporarily ignoring it.', failure.value.component)
            unreachable_host = yadtshell.components.UnreachableHost(
                failure.value.component)
            components[unreachable_host.uri] = unreachable_host
            return unreachable_host

        logger.critical(
            'ssh: cannot reach host %s\n\t System down? Passwordless SSH not '
            'configured? Network problems? Use --ignore-unreachable-hosts '
            'to ignore this error.',
            failure.value.component)

    return failure


def write_host_data_to_file(host, host_data):
    host = host.replace("host://", "")
    file_path = "%s.%s.status" % (yadtshell.settings.log_file, host)
    logger.debug("Status of %s is at %s" % (host, file_path))
    with open(file_path, "w") as status_file:
        status_file.write(host_data)


def create_host(protocol, components):
    if isinstance(protocol, yadtshell.components.AbstractHost):
        return protocol
    write_host_data_to_file(protocol.component, protocol.data)

    try:
        data = json.loads(protocol.data)
    except Exception, e:
        logger.debug('%s: %s, falling back to yaml parser' %
                     (protocol.component, str(e)))
        data = yaml.load(protocol.data, Loader=yaml_loader)

    host = None
    # simple data (just status) for backwards compat. with old yadtclient
    if data == yadtshell.settings.DOWN:
        # TODO(rwill): This is probably dead code since it does not set fqdn
        # and therefore creates invalid Host instance.
        host = yadtshell.components.Host(protocol.component)
        host.state = yadtshell.settings.DOWN
    elif data == yadtshell.settings.UNKNOWN:
        host = yadtshell.components.Host(protocol.component)
        host.state = yadtshell.settings.UNKNOWN
    elif data is None:
        logging.getLogger(protocol.component).warning('no data? strange...')
    elif "fqdn" not in protocol.data:
        logging.getLogger(protocol.component).warning(
            'no hostname? strange...')
    else:
        # note: this is actually the normal case
        host = yadtshell.components.Host(data['fqdn'])
        host.set_attrs_from_data(data)
    components[host.uri] = host
    return host


def initialize_services(host, components):
    """Find the service class for each of `host`s services and instantiate it.
    Return `host` to facilitate chaining.
    """
    if yadtshell.util.not_up(host.state):
        return host

    host.defined_services = []
    for name, settings in host.services.items():
        if settings is not None and "class" in settings:
            service_class_name = settings["class"]
        else:
            logger.debug("No service name found, using default: 'Service'")
            service_class_name = "Service"

        service_class = get_service_class_from_loaded_modules(
            service_class_name)
        if not service_class:
            service_class = get_service_class_from_fallbacks(
                host, service_class_name)

        service = None
        try:
            service = service_class(host, name, settings)
        except Exception, e:
            host.logger.exception(e)

        if not service:
            raise Exception(
                'cannot instantiate class %(service_class)s' % locals())

        components[service.uri] = service
        host.defined_services.append(service)
    return host


def get_service_class_from_loaded_modules(service_class_name):
    for module_name in sys.modules.keys()[:]:
        if module_name.startswith("six.moves"):
            continue  # six.moves is horrible and inspecting it causes a crash
        for classname, service_class in inspect.getmembers(sys.modules[module_name], inspect.isclass):
            if classname == service_class_name:
                return service_class
    return None


def get_service_class_from_fallbacks(host, service_class_name):
    host.logger.debug(
        '%s not a standard service, searching class' % service_class_name)
    service_class = None
    try:
        host.logger.debug('fallback 1: checking loaded modules')
        service_class = eval(service_class_name)
    except Exception:
        pass

    def get_class(service_class):
        module_name, class_name = service_class.rsplit('.', 1)
        host.logger.debug('trying to load module %s' % module_name)
        __import__(module_name)
        m = sys.modules[module_name]
        return getattr(m, class_name)

    if not service_class:
        try:
            host.logger.debug(
                'fallback 2: trying to load module myself')
            service_class = get_class(service_class_name)
        except Exception, e:
            host.logger.debug(e)
    if not service_class:
        try:
            # TODO(rwill): this might be dead code that can be removed
            host.logger.debug(
                'fallback 3: trying to lookup %s in legacies' % service_class_name)
            # this module is a config file living in /etc/yadtshell
            import legacies
            mapped_service_class = legacies.MAPPING_OLD_NEW_SERVICECLASSES.get(
                service_class_name, service_class_name)
            service_class = get_class(mapped_service_class)
            host.logger.info(
                'deprecation info: class %s was mapped to %s' %
                (service_class_name, mapped_service_class))
        except Exception, e:
            host.logger.debug(e)

    if not service_class:
        raise Exception(
            'cannot find class %(service_class)s' % locals())

    return service_class


def initialize_artefacts(host, components):
    for name_version in host.current_artefacts:
        add_artefact(
            components, host, name_version, yadtshell.settings.CURRENT)

    for name_version in host.next_artefacts:
        add_artefact(components, host, name_version, yadtshell.settings.NEXT)

    return host


def add_artefact(components, host, name_version, revision):
    name, version = name_version.split('/')
    artefact = yadtshell.components.Artefact(host, name, version, revision)
    components[artefact.uri] = artefact
    components[artefact.revision_uri] = artefact


def fetch_missing_hosts(ignored, components):
    missings = filter_missing_services(components)
    missing_deferreds = []
    for missing in missings:
        host = components.get("host://%s" % missing.host)
        if not host:
            logger.warn("%s on unknown host referenced" % missing.uri)
            d = query_status(missing.host, components)
            d.addCallbacks(callback=create_host, callbackArgs=[components],
                           errback=handle_failing_status, errbackArgs=[components])
            d.addErrback(yadtshell.twisted.report_error, logger.error)
            missing_deferreds.append(d)
    return defer.DeferredList(missing_deferreds, consumeErrors=True)


def fetch_missing_services_as_readonly(ignored, components):
    missings = filter_missing_services(components)
    missing_deferreds = []
    for missing in missings:
        host = components.get("host://%s" % missing.host,
                              yadtshell.components.Host(missing.host))
        readonly_service = yadtshell.components.ReadonlyService(
            host, missing.name)
        readonly_service.needed_by = missing.needed_by
        components[missing.uri] = readonly_service
        missing_deferreds.append(readonly_service.status())
    return defer.DeferredList(missing_deferreds, consumeErrors=True)


def handle_readonly_service_states(results, components):
    for success, protocol_or_failure in results:
        actual_state = yadtshell.settings.UP if success else yadtshell.settings.DOWN
        uri = protocol_or_failure.component.uri if success else protocol_or_failure.value.component.uri
        components[uri].state = actual_state
        logger.debug("Readonly status for %s : %s -> %s" % (uri, success, actual_state))


def status(hosts=None, include_artefacts=True, **kwargs):
    if type(hosts) is str:
        hosts = [hosts]

    try:
        os.remove(
            os.path.join(yadtshell.settings.OUT_DIR, 'current_state.components'))
    except OSError:
        pass

    if hosts:
        state_files = [os.path.join(yadtshell.settings.OUT_DIR, 'current_state_%s.yaml' % h)
                       for h in hosts]
    else:
        state_files = glob.glob(
            os.path.join(yadtshell.settings.OUT_DIR, 'current_state*'))
    for state_file in state_files:
        logger.debug('removing old state %(state_file)s' % locals())
        try:
            os.remove(state_file)
        except OSError, e:
            logger.warning('cannot remove %s:\n    %s' % (state_file, e))

    logger.debug('starting remote queries')

    # TODO(rwill): handle this case on caller side, remove "if hosts" above.
    if not hosts:
        hosts = yadtshell.settings.TARGET_SETTINGS['hosts']

    components = yadtshell.components.ComponentDict()

    def store_service_up(protocol):
        protocol.component.state = yadtshell.settings.UP
        return protocol

    def store_service_not_up(reason):
        reason.value.component.state = yadtshell.settings.STATE_DESCRIPTIONS.get(
            reason.value.exitCode, yadtshell.settings.UNKNOWN)
        return protocol

    def query_local_service(service):
        cmd = service.status()
        if isinstance(cmd, defer.Deferred):
            # TODO refactor: integrate all store_service_* cbs
            # TODO(rwill): possibly make those methods of Service, to simplify
            # to one line: cmd.addCallbacks(service.store_state,
            # service.handle_state_failure)
            def store_service_state(state, service):
                service.state = yadtshell.settings.STATE_DESCRIPTIONS.get(
                    state,
                    yadtshell.settings.UNKNOWN)
            cmd.addCallback(store_service_state, service)

            def handle_service_state_failure(failure, service):
                logger.debug('Failure while determining state of {0}. Exit code was {1}.'
                             .format(service.uri, failure.value.exitCode))
            cmd.addErrback(handle_service_state_failure, service)
            return cmd
        query_protocol = yadtshell.twisted.YadtProcessProtocol(
            service.uri, cmd)
        reactor.spawnProcess(
            query_protocol, '/bin/sh', ['/bin/sh'], os.environ)
        query_protocol.component = service
        query_protocol.deferred.addCallbacks(
            store_service_up, store_service_not_up)
        return query_protocol.deferred

    def add_local_state(host):
        local_state = []
        for service in getattr(host, 'defined_services', []):
            if getattr(service, 'state_handling', None) == 'serverside':
                if hasattr(service, 'prepare'):
                    service.prepare(host)
                if hasattr(service, 'get_local_service_collector'):
                    global local_service_collector
                    local_service_collector = service.get_local_service_collector(
                    )
                local_state.append(query_local_service(service))

        if local_state:
            dl = defer.DeferredList(local_state)
            dl.addCallback(lambda _: host)
            return dl
        return host

    def check_responses(responses):
        logger.debug('check_responses')
        logger.debug(responses)
        all_ok = True
        for ok, response in responses:
            if not ok:
                logger.debug("Found errored status response: %s" % response)
                all_ok = False
        if not all_ok:
            raise Exception('errors occured during status')

    def build_unified_dependencies_tree(ignored):
        logger.debug('building unified dependencies tree')

        for component in components.values():
            component.logger = None

        components._add_when_missing_ = True
        logger.debug('wiring components')
        for component in components.values():
            needs_with_resolved_version_alias = set([])
            for needed in getattr(component, 'needs', []):
                try:
                    needed_component = components[needed]
                    if not hasattr(needed_component, 'needed_by'):
                        needed_component.needed_by = set()
                    needed_component.needed_by.add(component.uri)
                    needs_with_resolved_version_alias.add(needed_component.uri)
                except (KeyError, AttributeError), e:
                    logger.debug('needed: ' + needed)
                    raise e

            component.needs = needs_with_resolved_version_alias
        components._add_when_missing_ = False

        for component in components.values():
            for dependent in getattr(component, 'needed_by', []):
                try:
                    dependent_component = components[dependent]
                    dependent_component.needs.add(component.uri)
                except KeyError, ke:
                    logger.warning("unknown dependent key " + str(ke))

        compute_dependency_scores(components)

    def store_status_locally(ignored, components):
        def _open_component_file(component_type):
            return open(os.path.join(yadtshell.settings.OUT_DIR, component_type), 'w')

        component_files = {
            yadtshell.settings.ARTEFACT: _open_component_file('artefacts'),
            yadtshell.settings.SERVICE: _open_component_file('services'),
            yadtshell.settings.HOST: _open_component_file('hosts'),
        }
        for component in components.values():
            component_files[component.type].write(component.uri + "\n")

        for f in component_files.values():
            f.close()

        f = _open_component_file('current_state.components')
        pickle.dump(components, f, pickle.HIGHEST_PROTOCOL)
        f.close()

        groups = []
        he = HostExpander()
        for grouped_hosts in yadtshell.settings.TARGET_SETTINGS['original_hosts']:
            hosts = []
            for hostname in he.expand(grouped_hosts):
                services = []
                host = components['host://%s' % hostname]

                host_services = getattr(host, 'defined_services', [])
                host_services.sort(key=lambda s: s.dependency_score)

                for service in host_services:
                    services.append({
                        'uri': service.uri,
                        'name': service.name,
                        'state': service.state
                    })

                artefacts = []
                for artefact in sorted(getattr(host, 'handled_artefacts', [])):
                    name, version = artefact.split('/')
                    artefacts.append({
                        'uri': 'artefact://%s/%s' % (hostname, name),
                        'name': name,
                        'current': version
                    })

                host = {
                    'name': hostname,
                    'services': services,
                    'artefacts': artefacts
                }
                hosts.append(host)
            groups.append(hosts)
        yadtshell.settings.ybc.sendFullUpdate(
            groups, tracking_id=yadtshell.settings.tracking_id)

        status_line = yadtshell.util.get_status_line(components)
        logger.debug('status: %s' % status_line)
        print(status_line)
        f = open(os.path.join(yadtshell.settings.OUT_DIR, 'statusline'), 'w')
        f.write('\n'.join(['', status_line]))
        f.close()

    def show_still_pending(deferreds):
        pending = [d.name for d in deferreds if not d.called]
        if pending:
            logger.info('pending: %s' % ' '.join(pending))
            reactor.callLater(10, show_still_pending, deferreds)

    def notify_collector(ignored):
        global local_service_collector
        if local_service_collector:
            logger.debug("collected services: %s " %
                         ", ".join(local_service_collector.services))
            return local_service_collector.notify()

    pi = yadtshell.twisted.ProgressIndicator()

    def query_and_initialize_host(hostname):
        deferred = query_status(hostname, components, pi)
        deferred.addCallbacks(callback=create_host,
                              callbackArgs=[components],
                              errback=handle_failing_status,
                              errbackArgs=[components, kwargs.get("ignore_unreachable_hosts")])

        deferred.addCallback(initialize_services, components)
        deferred.addCallback(add_local_state)
        deferred.addCallback(initialize_artefacts, components)
        deferred.addErrback(yadtshell.twisted.report_error, logger.error)
        return deferred

    deferreds = [query_and_initialize_host(host) for host in hosts]
    reactor.callLater(10, show_still_pending, deferreds)

    dl = defer.DeferredList(deferreds)
    dl.addCallback(check_responses)
    dl.addCallback(notify_collector)
    dl.addCallback(build_unified_dependencies_tree)
    dl.addCallback(fetch_missing_hosts, components)
    dl.addCallback(fetch_missing_services_as_readonly, components)
    dl.addCallback(handle_readonly_service_states, components)
    dl.addCallback(store_status_locally, components)
    dl.addCallback(yadtshell.info, components=components)
    dl.addErrback(yadtshell.twisted.report_error,
                  logger.error, include_stacktrace=False)

    return dl
