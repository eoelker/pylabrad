# Copyright (C) 2007  Matthew Neeley
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
labrad.node

Provides an interface to manage multiple labrad servers running on a particular
computer. The node runs each server in a separate process, so that they can not
interfere with each other. Information such as the manager host, port and
password are passed to the child process in environment variables or via
command line arguments. The startup process for each child server is controlled
by an associated .ini file.

The node name is read either from the LABRADNODE environment variable,
or failing that the system's hostname.

The node running on a particular computer is configured via the registry, at
the path ['', 'Nodes', node_name]. Defaults values can be configured to be
used whenever a new node connects, at ['', 'Nodes', '__default__']. The
configuration keys are:

  directories (*s): file directories to be scanned for servers. Any subdirs
                    that contain a file called '.nodeignore' will be skipped,
                    along with all their subdirs.
  extensions (*s): what files to look at, e.g. ['.ini', '.py', '.exe']
  autostart (*s): list of servers to be autostarted

Changes required on the server:

See "launchable-server.ini" for a description of how to have a server appear in
the node. See labrad-servers/Qubit Server/ for an example of a java server.

Another option is to have this configuration in the file itself, set off by
### BEGIN NODE INFO and ### END NODE INFO. See servers/gpibMockDeviceServer.py
in this repository for an example.

***N.B.*** The name given in the server's [info] section MUST MATCH the name
given as a class variable in the server class itself. That is, note that the
name string "GPIB Mock Device Server" appears _twice_. If this is not the case,
then while the node will still start the server, it will not register that the
server has been started and you will have to drop the server connection manually
to restart it.

Bonus: The node will also open up compiled executable files to look for
this information. This is how it works for the Direct Ethernet server, for
example.

The node can log to a file or via the syslog fascility.  Syslog can be
configured to filter messages generated by the node and its servers.
For rsyslogd on ubuntu, create the following file:

/etc/rsyslog.d/71-labrad.conf
:syslogtag,contains,"labrad"			/var/log/labrad.log;RSYSLOG_TraditionalFileFormat
"""

from __future__ import print_function

import logging
import logging.handlers
import os
import shlex
import socket
import sys
import zipfile
from datetime import datetime

from twisted.application.internet import TCPClient
from twisted.application.service import MultiService
from twisted.internet import defer, reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.error import ProcessDone, ProcessTerminated
from twisted.internet.protocol import ProcessProtocol
from twisted.python import usage
from twisted.python.runtime import platformType

import labrad
import labrad.support
from labrad import auth, protocol, util, types as T, constants as C
from labrad.node import server_config
from labrad.server import LabradServer, setting
from labrad.util import DeferredSignal, interpEnvironmentVars, mux


# Maximum number of lines of stdout to keep per server.
LOG_LENGTH = 1000


# Named message fired by the manager when new servers connect.
SERVER_CONNECTED = 'Server Connect'


class ServerProcess(ProcessProtocol):
    """A class to represent a running server instance."""

    # Messages that will be fired when we transition to various statuses
    STATUS_MESSAGES = {
        'STARTING': 'server_starting',
        'STARTED': 'server_started',
        'STOPPING': 'server_stopping',
        'STOPPED': 'server_stopped',
    }

    def __init__(self, config, env, client, on_message):
        """Initialize a ServerProcess instance.

        Args:
            config (server_config.ServerConfig): Configuration information
                describing how the node should launch this process.
            env (Dict[str, str]): Map of environment variables that should be
                passed to the subprocess. These will override variables in the
                environment of the node process itself.
            client (labrad.wrappers.ClientAsync): Client connection to labrad.
            on_message (Callable[[ServerProcess, str], None]): Called when state
                changes happen for this server process, such as when it starts
                and stops. Will be called with a reference to this ServerProcess
                instance and a string giving the state change.
        """
        self.config = config
        self.env = env
        self.full_env = os.environ.copy()
        self.full_env.update(env)
        if config.filename:
            self.full_env['FILE'] = config.filename
        if config.path:
            self.full_env['DIR'] = config.path
        self.client = client
        self.name = interpEnvironmentVars(config.instance_name, self.full_env)
        self.args = shlex.split(config.cmdline)
        self.args = [interpEnvironmentVars(a, self.full_env) for a in self.args]
        self.status = None
        self.output = []
        self.on_message = on_message
        self._lock = defer.DeferredLock()
        logname = 'labrad.' + labrad.support.mangle(self.name)
        self.logger = logging.getLogger(logname)

        # Signal that will fire when the server process is shutdown.
        self.on_shutdown = DeferredSignal()

    @property
    def server_name(self):
        return self.config.name

    @property
    def path(self):
        return self.config.path or os.curdir()

    @property
    def timeout(self):
        return self.config.timeout

    @property
    def executable(self):
        """Get the executable to pass to twisted's spawnProcess method.

        On Unix, spawnProcess calls fork() and then execvpe which expects an
        executable name and a series of arguments, and tries to resolve the
        executable using PATH. On windows, it calls createProcess, which expects
        a command string and an optional executable. If the executable argument
        is present, it must be an absolute path including .exe suffix. If
        executable is not present, the first part of the command string is used,
        and invokes the %PATH% and extension search.

        Twisted spawnProcess only supports posix (including Mac) and win32, so
        we just abandon anything else. The relevant twisted code is:

            twisted/internet/process.py:_BaseProcess
            twisted/internet/dumbwin32proc.py:Process

        Note that the twisted docs for spawnProcess say that the full executable
        path is required, but this is not in fact the case. See:

            http://twistedmatrix.com/documents/current/api/twisted.internet.interfaces.IReactorProcess.spawnProcess.html
        """
        if platformType == 'posix':
            return self.args[0]
        elif platformType == 'win32':
            return None
        else:
            raise RuntimeError("Unsupported platform %s" % platformType)

    def set_status(self, value):
        """Transition to the given status and notify message listeners."""
        message = self.STATUS_MESSAGES[value]
        self.status = value
        self.logger.info(value)
        self.on_message(self, message)

    @inlineCallbacks
    def start(self):
        self.set_status('STARTING')
        self.logger.info("path: {}".format(self.path))
        self.logger.info("args: {}".format(self.args))

        msg_ctx = self.client.context()
        msg_id = msg_ctx[1]

        connected = defer.Deferred()

        def on_server_connect(message_ctx, msg):
            """Handler that will be called when labrad servers connect.

            If we see a server whose name matches the one we are trying to
            start, we assume that the server process we spawned has successfully
            connected. So, we fire the `connected` Deferred.
            """
            _ID, name = msg
            if name == self.name:
                connected.callback(None)

        # start listening for server connect messages
        manager = self.client.manager
        manager.addListener(on_server_connect, context=msg_ctx)
        yield manager.subscribe_to_named_message(
            SERVER_CONNECTED, msg_id, True, context=msg_ctx)

        # start the server process
        self.proc = reactor.spawnProcess(self, self.executable, self.args,
                                         env=self.full_env, path=self.path)

        # wait for the server to connect to labrad, shutdown, or timeout,
        # whichever comes first.
        selected = yield mux.select({
            'connected': connected,
            'shutdown': self.on_shutdown(),
            'timeout': mux.after(self.timeout)
        })

        # stop listening for server connect messages
        try:
            manager.removeListener(on_server_connect, context=msg_ctx)
            yield manager.subscribe_to_named_message(
                SERVER_CONNECTED, msg_id, False, context=msg_ctx)
        except Exception:
            self.logger.info('Error while unsubscribing from labrad messages',
                             exc_info=True)

        if selected.key == 'timeout':
            yield self._kill()
            raise T.Error('Failed to connect to labrad in {} seconds.'
                          .format(self.timeout), payload=self.output)
        if selected.key == 'shutdown':
            raise T.Error('Process exited before connecting to labrad.',
                          payload=self.output)

        # we're connected!
        self.set_status('STARTED')

    def stop(self):
        """Stop this server instance.

        We protect this with a lock so that multiple calls to stop the server
        will not interfere with each other. Rather, subsequent calls to stop
        will wait for the initial call to finish before returning.
        """
        return self._lock.run(self._stop)

    @inlineCallbacks
    def _stop(self):
        if self.status == 'STOPPED':
            return

        self.set_status('STOPPING')

        if self.config.shutdown_mode is not None:
            # Try to do an orderly shutdown.
            mode, ID = self.config.shutdown_mode
            if mode == 'message':
                try:
                    self.client.servers[self.name].sendMessage(ID)
                except:
                    self.logger.info('Error while shutting down with message',
                                     exc_info=True)
            elif mode == 'setting':
                try:
                    yield self.client.servers[self.name][ID]()
                except:
                    self.logger.info('Error while shutting down with setting',
                                     exc_info=True)

            selected = yield mux.select({
                'shutdown': self.on_shutdown(),
                'timeout': mux.after(self.config.shutdown_timeout)
            })

            if selected.key == 'shutdown':
                return

        # Shutdown failed or not configured, so just kill the process.
        yield self._kill()

    @inlineCallbacks
    def _kill(self):
        """Send a signal to kill the subprocess and wait for it to exit."""
        try:
            self.proc.signalProcess('KILL')
        except:
            self.logger.error('Error killing subprocess', exc_info=True)
        yield self.on_shutdown()

    # ProcessProtocol callbacks called by the subprocess.

    def processEnded(self, reason):
        """Called when the server process ends.

        We set the status to STOPPED and call the shutdown callback to notify
        anyone waiting for us to shutdown.
        """
        if isinstance(reason.value, ProcessDone):
            self.logger.info("process closed cleanly.")
        elif isinstance(reason.value, ProcessTerminated):
            self.logger.info("process terminated: {}".format(reason.value))
        else:
            self.logger.info("process ended: {}".format(reason))
        self.set_status('STOPPED')
        self.on_shutdown.callback()

    def outReceived(self, data):
        """Called when the server prints to stdout."""
        self.output.append((datetime.now(), data))
        self.output = self.output[-LOG_LENGTH:]
        self.logger.info(data.strip())

    def errReceived(self, data):
        """Called when the server prints to stderr."""
        self.output.append((datetime.now(), data))
        self.output = self.output[-LOG_LENGTH:]
        self.logger.warning(data.strip())

    def clearOutput(self):
        """Clear the log of stdout."""
        self.output = []


class Node(object):
    """Parent class that keeps the node running.

    If the manager is stopped or we lose the network connection,
    this service attempts to restart it so that we will come
    back online when the manager is back up.
    """
    reconnectDelay = 10

    def __init__(self, nodename, host, port, username, password,
                 tls_mode=C.MANAGER_TLS):
        self.nodename = nodename
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.tls_mode = tls_mode

    @inlineCallbacks
    def run(self):
        """Run the node in a loop, reconnecting after connection loss."""
        log = logging.getLogger('labrad.node')
        while True:
            print('Connecting to {}:{}...'.format(self.host, self.port))
            try:
                p = yield protocol.connect(self.host, self.port, self.tls_mode,
                                           self.username, self.password)
                node = NodeServer(self.nodename, self.host, self.port,
                                  p.credential)
                yield node.startup(p)
            except Exception:
                log.error('Node failed to start', exc_info=True)
            else:
                try:
                    yield node.onShutdown()
                except Exception:
                    log.error('Error during node shutdown', exc_info=True)

            yield util.wakeupCall(0)
            print('Will try to reconnect in {} seconds...'.format(self.reconnectDelay))
            yield util.wakeupCall(self.reconnectDelay)


class NodeConfig(object):
    """Load configuration from the registry and monitor it for changes.

    Attributes:
        dirs (list(string)): a list of directories that will be searched for
            runnable servers.
        exts (list(string)): a list of file extensions that will be included
            when searching for servers.
        autostart (list(string)): a list of servers that will be automatically
            started when the node launches.
    """

    @classmethod
    @inlineCallbacks
    def create(cls, parent):
        """Loads node configuration from the registry."""
        instance = cls(parent)
        yield instance._init()
        returnValue(instance)

    def __init__(self, parent):
        self.parent = parent
        self.nodename = parent.nodename
        cxn = parent.client
        self._cxn = cxn
        self._reg = cxn.registry
        self._ctx = cxn.context()

    @inlineCallbacks
    def _init(self):
        """Initialize by loading from the registry.

        Copy from the default directory, creating it if necessary.
        Also, set up messages to monitor the config directory for
        changes.
        """
        p = self._packet()
        p.cd(['', 'Nodes'], True)
        p.dir()
        ans = yield p.send()
        dirs, keys = ans.dir

        # load defaults (creating them if necessary)
        create = '__default__' not in dirs
        defaults = ([], ['.ini', '.py'], [])
        defaults = yield self._load('__default__', create, defaults)

        # load this node (creating config if necessary)
        create = self.nodename not in dirs
        config = yield self._load(self.nodename, create, defaults)
        self._update(config, False)

        # setup messages when registry changes
        self._reg.addListener(self._handleMessage, context=self._ctx)
        p = self._packet()
        p.notify_on_change(2345, True)
        yield p.send()

    def _packet(self):
        """Create a packet to the registry server in our context."""
        return self._reg.packet(context=self._ctx)

    def _update(self, config, triggerRefresh=True):
        """Update instance variables from loaded config."""
        self.dirs, self.extensions, self.autostart = config
        print('config updated: dirs={}, extensions={}, autostart={}'.format(
        self.dirs, self.extensions, self.autostart))
        if triggerRefresh:
            self.parent.refreshServers()

    @inlineCallbacks
    def _load(self, nodename=None, create=False, defaults=None):
        """Load the current configuration out of the registry."""
        p = self._packet()
        if nodename is not None:
            p.cd(['', 'Nodes', nodename], True)
        if create:
            p.set('directories', defaults[0])
            p.set('extensions', defaults[1])
            p.set('autostart', defaults[2])
        p.get('directories', '*s', key='dirs')
        p.get('extensions', '*s', key='exts')
        p.get('autostart', '*s', True, [], key='autostart')
        ans = yield p.send()
        def remove_empties(strs):
            return [s for s in strs if s]
        dirs = remove_empties(ans.dirs)
        exts = remove_empties(ans.exts)
        autostart = sorted(remove_empties(ans.autostart))
        returnValue((dirs, exts, autostart))

    def _save(self):
        """Save the current configuration to the registry."""
        p = self._packet()
        p.set('directories', self.dirs)
        p.set('extensions', self.extensions)
        return p.send()

    @inlineCallbacks
    def _handleMessage(self, c, msg):
        """Reload when we get a message from the registry."""
        try:
            config = yield self._load()
            self._update(config)
        except Exception:
            logging.error('Error in _handleMessage', exc_info=True)

    @inlineCallbacks
    def update_autostart(self, autostart):
        """Update the list of servers to be autostarted.

        Args:
            autostart (list(string)): New list of autostart server names. This
                will completely replace the current list.
        """
        p = self._packet()
        p.cd(['', 'Nodes', self.nodename])
        p.set('autostart', sorted(autostart))
        yield p.send()


class NodeServer(LabradServer):
    """Start and stop LabRAD servers remotely.

    The node server allows you to control and
    monitor servers running on a remote machine.
    """

    name = 'node %LABRADNODE%'

    def __init__(self, nodename, host, port, credential):
        """Initialize a NodeServer instance.

        Args:
            nodename (str): The name of this node, e.g. from the LABRADNODE
                environment variable.
            host (str): The host where the labrad manager is running.
            port (int): The port where the labrad manager is running.
            credential (labrad.auth.Password): Credentials for connecting to the
                labrad manager.
        """
        LabradServer.__init__(self)
        self.nodename = nodename
        self.name = 'node %s' % nodename
        self.host = host
        self.port = port
        self.credential = credential

        # Mapping from server name to ServerConfig that describes how to launch
        # instances of the server.
        self.server_configs = {}

        # Mapping from instance name to ServerProcess object. Instances are
        # added to this mapping when they are successfully started and removed
        # when they send a message indicating that the server process has
        # stopped.
        self.instances = {}

    @inlineCallbacks
    def initServer(self):
        """Initialize this server after connecting to the labrad manager."""
        self.config = yield NodeConfig.create(self)
        self.refreshServers()
        self.autostart(None)

    def stopServer(self):
        """Stop this node by killing all subprocesses."""
        stoppages = [inst.stop() for inst in self.instances.values()]
        return defer.DeferredList(stoppages)


    # message handling

    def on_subprocess_message(self, sender, message):
        """Called when a subprocess sends a message.

        Args:
            sender (ServerProcess): The subprocess that sent the message.
            message (str): Message describing the change in the subprocess
                state, such as 'server_started', 'server_stopped', etc.
                If the server stopped, we remove the sending instance from the
                instances map. Other messages will be sent out as labrad named
                messages to notify interested listeners, such as the node web
                interface.
        """
        self._relayMessage(message, server=sender.server_name,
                           instance=sender.name)

    def _relayMessage(self, signal, **kw):
        """Send messages out to LabRAD."""
        kw['node'] = self.name
        mgr = self.client.manager
        mgr.send_named_message('node.' + signal, tuple(kw.items()))


    # status information

    def status(self):
        """Get information about all servers on this node."""
        def server_info(config):
            instance_names = [inst.name for inst in self.instances.values()
                                        if inst.server_name == config.name and
                                           inst.status == 'STARTED']
            return (config.name, config.description or '', config.version,
                    config.instance_name, config.environ_vars, instance_names)

        return [server_info(config)
                for _name, config in sorted(self.server_configs.items())]


    # server refresh

    def refreshServers(self):
        """Refresh the list of available servers."""
        # configs is a nested map from name to version to list of classes.
        #
        # This allows us to deal with cases where there are many definitions
        # for different server versions, and possibly also redundant defitions
        # for the same version.
        configs = {}

        # look for .ini files
        for dirname in self.config.dirs:
            for path, dirs, files in os.walk(dirname):
                if '.nodeignore' in files:
                    del dirs[:] # clear dirs list so we don't visit subdirs
                    continue
                for f in files:
                    try:
                        _, ext = os.path.splitext(f)
                        if ext.lower() not in self.config.extensions:
                            continue
                        if ext.lower() == '.ini':
                            with open(os.path.join(path, f)) as file:
                                conf = file.read()
                        elif ext.lower() in ['.war', '.jar']:
                            zf = zipfile.ZipFile(os.path.join(path, f))
                            found = False
                            for info in zf.infolist():
                                if 'node.ini' in info.filename:
                                    found = True
                                    member = zf.open(info.filename)
                                    conf = member.read()
                                    member.close()
                                    break
                            zf.close()
                        else:
                            conf = server_config.find_config_block(path, f)
                            if conf is None:
                                continue
                        config = server_config.from_string(conf, f, path)
                        versions = configs.setdefault(config.name, {})
                        versions.setdefault(config.version, []).append(config)
                    except Exception:
                        fname = os.path.join(path, f)
                        logging.error('Error while loading config file "%s":' % fname,
                                  exc_info=True)

        server_configs = {}
        for versions in configs.values():
            for servers in versions.values():
                if len(servers) > 1:
                    conflicting_files = [s.filename for s in servers]
                    s = servers[0]
                    logging.warning(
                        'Found redundant server configs with same name and '
                        'version; will use {}. name={}, version={}, '
                        'conflicting_files={}'
                        .format(s.filename, s.name, s.version,
                                conflicting_files))

            servers = [ss[0] for ss in versions.values()]
            servers.sort(key=lambda s: s.version_tuple)
            if len(servers) > 1:
                # modify server name for all but the latest version
                for s in servers[:-1]:
                    s.name = '{}-{}'.format(s.name, s.version)

            for s in servers:
                server_configs[s.name] = s
        self.server_configs = server_configs
        # send a message with the current server list
        self._relayMessage('status', servers=self.status())


    # LabRAD settings

    @setting(1, name='s', environ='*(ss)', returns='s')
    def start(self, c, name, environ={}):
        """Start an instance of a server."""
        if name not in self.server_configs:
            raise Exception("Unknown server: '%s'." % name)
        environ = dict(environ) # copy context environment
        environ.update(LABRADNODE=self.nodename,
                       LABRADHOST=self.host,
                       LABRADPORT=str(self.port),
                       PYTHON=sys.executable)
        if isinstance(self.credential, auth.Password):
            environ.update(LABRADUSER=self.credential.username,
                           LABRADPASSWORD=self.credential.password)
        config = self.server_configs[name]
        srv = ServerProcess(config, environ, client=self.client,
                            on_message=self.on_subprocess_message)
        instance_name = srv.name
        if instance_name in self.instances:
            raise Exception("Server '%s' already running." % instance_name)
        yield srv.start()
        self.instances[instance_name] = srv

        # remove instance from instance dict when it shuts down
        @inlineCallbacks
        def handle_shutdown():
            yield srv.on_shutdown()
            self._remove_instance(instance_name)
        handle_shutdown()

        returnValue(instance_name)

    @setting(2, instance_name='s', returns='s')
    def stop(self, c, instance_name):
        """Stop a running server instance."""
        yield self._stop(instance_name)
        returnValue(instance_name)

    @setting(3, instance_name='s', returns='s')
    def restart(self, c, instance_name):
        """Restart a running server instance."""
        inst = yield self._stop(instance_name)
        yield self.start(c, inst.server_name, inst.env)
        returnValue(instance_name)

    @inlineCallbacks
    def _stop(self, instance_name):
        """Stop a running server instance, and return the instance."""
        if instance_name not in self.instances:
            raise Exception("'%s' is not running." % instance_name)
        inst = self.instances[instance_name]
        yield inst.stop()
        # ensure instance is removed from instance dict before we return
        self._remove_instance(instance_name) 
        returnValue(inst)

    def _remove_instance(self, instance_name):
        if instance_name in self.instances:
            del self.instances[instance_name]

    @setting(10, returns='*s')
    def available_servers(self, c):
        """Get a list of available servers."""
        return sorted(self.server_configs.keys())

    @setting(11, returns='*(ss)')
    def running_servers(self, c):
        """Get a list of running server instances.

        Returns a list of tuples of server name and instance name.
        """
        return sorted((s.server_name, s.name)
                      for s in self.instances.values() if s.status == 'STARTED')

    @setting(12, returns='*s')
    def local_servers(self, c):
        """Get a list of local servers."""
        return sorted(n for n, s in self.server_configs.items() if s.isLocal)

    @setting(13, returns='')
    def refresh_servers(self, c):
        """Refresh the list of available servers."""
        self.refreshServers()

    @setting(14, 'status',
             returns='*(s{name} s{desc} s{ver} s{instname} *s{vars} *s{running})')
    def get_status(self, c):
        """Get information about all servers on this node."""
        return self.status()

    @setting(100, instance_name='s', returns='*(ts)')
    def server_output(self, c, instance_name):
        """Get output from a server's stdout."""
        if instance_name not in self.instances:
            raise Exception("'%s' is not running." % instance_name)
        return self.instances[instance_name].output

    @setting(101, instance_name='s', returns='')
    def clear_output(self, c, instance_name):
        """Clear the stdout buffer of a server."""
        if instance_name not in self.instances:
            raise Exception("'%s' is not running." % instance_name)
        self.instances[instance_name].clearOutput()

    @setting(102, name='s', returns='s')
    def server_version(self, c, name):
        """Get version information for a server."""
        if name not in self.server_configs:
            raise Exception("'%s' not found." % name)
        return self.server_configs[name].version

    @setting(103, name='s', enable='b', returns='')
    def stream_output(self, c, name, enable):
        """Enable or disable server output messages.

        This allows you to receive messages whenever a server
        outputs something on its stdout, effectively giving a
        remote view of the server's console window.
        """

    @setting(200, returns='')
    def autostart(self, c):
        """Start all servers from the configured autostart list.

        Any servers that are already running will be left as is, while those
        that are not yet running will be started. Autostart is triggered when
        the node first starts up, but can be invoked manually at any time
        thereafter.
        """
        running = set(s.server_name for s in self.instances.values())
        to_start = [name for name in self.config.autostart
                         if name not in running]
        deferreds = [(name, self.start(c, name)) for name in to_start]
        for name, deferred in deferreds:
            try:
                yield deferred
            except Exception:
                logging.error('Failed to autostart "%s"', name, exc_info=True)

    @setting(201, returns='*s')
    def autostart_list(self, c):
        """Get the list of servers that are configured to be autostarted."""
        return self.config.autostart

    @setting(202, name='s', returns='')
    def autostart_add(self, c, name):
        """Add a server to the autostart list."""
        if name not in self.server_configs:
            raise Exception("Unknown server: '%s'." % name)
        autostart = set(self.config.autostart)
        autostart.add(name)
        yield self.config.update_autostart(sorted(autostart))

    @setting(203, name='s', returns='')
    def autostart_remove(self, c, name):
        """Remove a server from the autostart list."""
        autostart = set(self.config.autostart)
        try:
            autostart.remove(name)
        except KeyError:
            pass
        yield self.config.update_autostart(sorted(autostart))

    @setting(1000, returns='*(ss)')
    def node_version(self, c):
        """Return a list of key-value tuples containing info about this node."""
        info = {
            'hostname': socket.gethostname(),
            'nodename': self.nodename,
            'python version': sys.version,
            'labrad version': labrad.__version__,
            'labrad revision': labrad.__revision__,
            'labrad date': labrad.__date__,
            }
        return list(info.items())


class NodeOptions(usage.Options):
    optParameters = [
            ['name', 'n', util.getNodeName(), 'Node name.'],
            ['host', 'h', C.MANAGER_HOST, 'Manager location.'],
            ['port', 'p', C.MANAGER_PORT, 'Manager port.'],
            ['username', 'u', None, 'Login username.'],
            ['password', 'w', None, 'Login password.'],
            ['tls', '', C.MANAGER_TLS,
             'TLS mode for connecting to manager (on/starttls/off)'],
            ['logfile', 'l', None, 'Enable logging to a file'],
            ['syslog_socket', 'x', None,
             'Override default syslog socket. Absolute path or host[:port]']]
    optFlags = [['syslog', 's', 'Enable syslog'],
                ['verbose', 'v', 'Enable debug output']]


def makeService(options):
    """Construct a TCPServer from a LabRAD node."""
    name = options['name']
    host = options['host']
    port = int(options['port'])
    username = options['username']
    password = options['password']
    tls_mode = C.check_tls_mode(options['tls'])
    return Node(name, host, port, username, password, tls_mode)


def setup_logging(options):
    logging.basicConfig()
    node_log = logging.getLogger('labrad')
    if options['syslog']:
        # We need to find the path to the system log socket, which varies by
        # platform. Linux and OS/X defaults are listed below. On windows the
        # only option is UDP logging, but since UDP is connectionless there is
        # no way to tell if there is actually a syslog daemon listening.
        # https://docs.python.org/2/library/logging.handlers.html#sysloghandler
        if options['syslog_socket']:
            if '/' in options['syslog_socket']:
                address = options['syslog_socket']
            else:
                host, _, port = options['syslog_socket'].partition(':')
                if port == '':
                    address = (host, 514)
                else:
                    address = (host, int(port))
        elif sys.platform.startswith('linux'):
            address = '/dev/log'
        elif sys.platform.startswith('darwin'):
            address = '/var/run/syslog'
        else:
            node_log.critical(
                    'Syslog specified, but default socket not known for '
                    'platform {}. Use -s option'.format(sys.platform))
            sys.exit(1)
        syslog_handler = logging.handlers.SysLogHandler(address=address)
        syslog_handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))
        node_log.addHandler(syslog_handler)
    if options['logfile']:
        file_handler = logging.handlers.RotatingFileHandler(
                options['logfile'], maxBytes=800000, backupCount=5)
        formatter = logging.Formatter('%(asctime)s - %(name)s: %(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')
        file_handler.setFormatter(formatter)
        node_log.addHandler(file_handler)
    if options['verbose']:
        node_log.setLevel(logging.DEBUG)
    else:
        node_log.setLevel(logging.INFO)

def main():
    config = NodeOptions()
    config.parseOptions()
    setup_logging(config)
    logging.getLogger('labrad.node').info('Starting')
    service = makeService(config)
    service.run()
    reactor.run()

if __name__ == '__main__':
    main()
