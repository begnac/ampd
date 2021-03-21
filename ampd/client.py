# coding: utf-8

# Asynchronous Music Player Daemon client library for Python

# Copyright (C) 2015 Itaï BEN YAACOV

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import os
import logging
import asyncio
import urllib.parse
import sys
import decorator
import traceback
import enum

from . import request
from . import errors


_logger = logging.getLogger(__name__.split('.')[0])


class _Task(asyncio.Task):
    """
    Wrapper for AMPD tasks.
    """
    def __init__(self, future, *, loop=None):
        self._caller_filename, self._caller_line, self._caller_function, self._caller_text = traceback.extract_stack()[-4]
        self._future = future
        super().__init__(self.wrap(), loop=loop)

    async def wrap(self):
        try:
            await self._future
        except asyncio.CancelledError:
            pass
        except Exception:
            print("While awaiting AMPD task {}:".format(self._future))
            sys.excepthook(*sys.exc_info())

    def _repr_info(self):
        info = super()._repr_info()
        return info[:1] + [repr(self._future)] + info[2:]


@decorator.decorator
def task(func, *args, **kwargs):
    """
    Decorator for AMPD task functions.

    Wraps in a Task which will accept cancellation as normal termination.
    """
    return _Task(func(*args, **kwargs))


class AMPDProtocol(asyncio.Protocol):
    def __init__(self, process_reply, disconnect_cb):
        super().__init__()
        self._process_reply = process_reply
        self._disconnect_cb = disconnect_cb

    def connection_made(self, transport):
        _logger.debug("Protocol connection made")
        super().connection_made(transport)
        self._transport = transport
        self._lines = []
        self._incomplete_line = b''

    def connection_lost(self, exc):
        _logger.debug("Protocol connection lost")
        del self._transport
        if self._disconnect_cb is not None:
            asyncio.ensure_future(self._disconnect_cb(Client.DISCONNECT_ERROR))
        super().connection_lost(exc)

    def data_received(self, data):
        new_lines = (self._incomplete_line + data).split(b'\n')
        for line in new_lines[:-1]:
            self._lines.append(line)
            if line.startswith(b'OK') or line.startswith(b'ACK'):
                asyncio.ensure_future(self._process_reply(self._lines))
                self._lines = []
        self._incomplete_line = new_lines[-1]


class Executor(object):
    """
    Generates AMPD requests.
    """

    def __init__(self, client_or_parent):
        if isinstance(client_or_parent, Executor):
            self._parent = client_or_parent
            self._client = client_or_parent._client
            self._parent._children.append(self)
        else:
            self._parent = None
            self._client = client_or_parent
        self._children = []
        self._requests = []
        self._connect_cb_func = self._disconnect_cb_func = None

    def close(self):
        _logger.debug("Closing executor {}".format(self))
        if not self._client:
            return
        while self._children:
            self._children[0].close()
        if self._requests:
            for request_ in self._requests:
                request_.cancel()
        if self._parent:
            self._parent._children.remove(self)
            self._parent = None
        self._client = None
        self._connect_cb_func = self._disconnect_cb_func = None
        _logger.debug("Executor closed")

    def sub_executor(self):
        "Return a child Executor."
        return Executor(self)

    def set_callbacks(self, connect_cb, disconnect_cb):
        self._connect_cb_func = connect_cb
        self._disconnect_cb_func = disconnect_cb
        if self.get_is_connected() and connect_cb is not None:
            connect_cb()

    def _connect_cb(self):
        if self._connect_cb_func is not None:
            self._connect_cb_func()
        for child in self._children:
            child._connect_cb()

    def _disconnect_cb(self, reason, message):
        for child in self._children:
            child._disconnect_cb(reason, message)
        if self._disconnect_cb_func is not None:
            self._disconnect_cb_func(reason, message)

    def get_is_connected(self):
        return self._client._state & ClientState.FLAG_CONNECTED

    def get_protocol_version(self):
        return self._client.protocol_version

    def __getattr__(self, name):
        return request.Request._new_request(self, name)

    def _log_request(self, request_):
        if self._client is None:
            raise errors.ConnectionError
        _logger.debug("Appending request {} of task {} to {}".format(request_, asyncio.current_task(), self))
        self._requests.append(request_)
        request_.add_done_callback(self._unlog_request)
        if isinstance(request_, request.RequestPassive):
            self._client._wait(request_)
        else:
            self._client._send(request_)

    def _unlog_request(self, request_):
        self._requests.remove(request_)


class ClientState(enum.IntFlag):
    FLAG_CONNECTED = 1
    FLAG_ACTIVE = 2

    STATE_DISCONNECTED = 0
    STATE_CONNECTING = FLAG_ACTIVE
    STATE_IDLE = FLAG_CONNECTED
    STATE_ACTIVE = FLAG_CONNECTED | FLAG_ACTIVE


class Client(object):
    """
    Establishes connection with the MPD server.
    """

    DISCONNECT_NOT_CONNECTED = 0
    DISCONNECT_FAILED_CONNECT = 1
    DISCONNECT_ERROR = 2
    DISCONNECT_REQUESTED = 3
    DISCONNECT_RECONNECT = 4
    DISCONNECT_SHUTDOWN = 5
    DISCONNECT_PASSWORD = 6

    def __init__(self, *, excepthook=None):
        """
        Initialize a client.

        excepthook - override sys.excepthook for exceptions raised in workers.
        """
        self.executor = Executor(self)
        self._excepthook = excepthook
        self._waiting_list = []
        self._host = self._port = self._password = None

        self._state = ClientState.STATE_DISCONNECTED
        self.protocol_version = None

        self._run_lock = asyncio.Lock()

    def __del__(self):
        _logger.debug("Deleting {}".format(self))

    async def close(self):
        """
        Close all executors, disconnect from server.
        """
        _logger.debug("Closing client")
        await self.disconnect_from_server(self.DISCONNECT_SHUTDOWN)
        self.executor.close()
        _logger.debug("Client closed")

    async def connect_to_server(self, host=None, port=6600, password=None):
        """
        host     - '[password@]hostname[:port]'.  Default to $MPD_HOST or 'localhost'.
        port     - Ignored if given in the 'host' argument.
        password - Ignored if given in the 'host' argument.
        """

        netloc = urllib.parse.urlsplit('//' + (host or os.environ.get('MPD_HOST', 'localhost')))

        self._host = netloc.hostname
        self._port = netloc.port or port
        self._password = netloc.username or password

        await self.reconnect_to_server()

    async def reconnect_to_server(self):
        """
        Connect to server with previous host / port / password.
        """
        await self.disconnect_from_server(self.DISCONNECT_RECONNECT)
        self._connecting = asyncio.current_task()
        self._state = ClientState.STATE_CONNECTING

        try:
            _logger.debug("Connecting to {}:{}".format(self._host, self._port))
            self._transport, self._protocol = await asyncio.get_event_loop().create_connection(self._protocol_factory, self._host, self._port)
            _logger.debug("Connected")
        except OSError as exc:
            self._state = ClientState.STATE_DISCONNECTED
            del self._connecting
            self.executor._disconnect_cb(self.DISCONNECT_FAILED_CONNECT, str(exc))
            del exc
            return

        self._state = ClientState.STATE_ACTIVE
        del self._connecting
        welcome = request.RequestWelcome(self.executor)
        self._active_queue = [welcome]
        self.protocol_version = await welcome
        if self._password:
            try:
                await self.executor.password(self._password)
            except errors.ReplyError:
                self.disconnect_from_server(self.DISCONNECT_PASSWORD)
                return
        self.executor._connect_cb()
        self._event(request.Event.CONNECT)

    async def disconnect_from_server(self, _reason=DISCONNECT_REQUESTED, _message=None):
        if self._state == ClientState.STATE_DISCONNECTED:
            return

        if self._state == ClientState.STATE_CONNECTING:
            self._connecting.cancel()
        else:
            self._protocol._disconnect_cb = None
            self._transport.close()
            self._transport._read_ready_cb = None  # Should be done in selector_events.py
            self.protocol_version = None
            del self._active_queue, self._transport, self._protocol
            _logger.debug("Disconnected, deleted")

        self._state = ClientState.STATE_DISCONNECTED
        self.executor._disconnect_cb(_reason, _message)

    async def disconnect_from_server_async(self, _reason=DISCONNECT_REQUESTED, _message=None):
        self.disconnect_from_server(_reason, _message)

    async def _process_reply(self, reply):
        assert '_active_queue' in vars(self)
        if not self._active_queue:
            self.disconnect_from_server(self.DISCONNECT_ERROR)
            return

        request_ = self._active_queue.pop(0)
        request_._process_reply(reply)
        if not self._active_queue:
            self._active = False
            self._idle_task()

    def _protocol_factory(self):
        return AMPDProtocol(self._process_reply, self.disconnect_from_server_async)

    def _send(self, request_):
        self._active = True
        if not self._state & ClientState.FLAG_CONNECTED:
            raise errors.ConnectionError
        if isinstance(request_, request.RequestIdle):
            self._state &= ~ClientState.FLAG_ACTIVE
        elif not self._state & ClientState.FLAG_ACTIVE:
            self._transport.write(b'noidle\n')
            _logger.debug("Unidle")
            self._state |= ClientState.FLAG_ACTIVE
        self._transport.write(request_._commandline.encode('utf-8') + b'\n')
        _logger.debug("Write : " + request_._commandline)
        self._active_queue.append(request_)

    def _wait(self, request_):
        event = self._current_events() & request_._event_mask
        if event:
            request_.set_result(event)
        else:
            self._waiting_list.append(request_)
            request_.add_done_callback(self._waiting_list.remove)

    def _current_events(self):
        idle = request.Event.IDLE if self._state == ClientState.STATE_IDLE else request.Event(0)
        connected = request.Event.CONNECT if self._state & ClientState.FLAG_CONNECTED else request.Event(0)
        return idle | connected

    @task
    async def _connect_task(self, welcome):
        self.protocol_version = await welcome
        if self._password:
            try:
                await self.executor.password(self._password)
            except errors.ReplyError:
                self.disconnect_from_server(self.DISCONNECT_PASSWORD)
                return
        self.executor._connect_cb()
        self._event(request.Event.CONNECT)

    def _unidle(self, request_):
        if self._state & ClientState.FLAG_CONNECTED:
            self._state |= ClientState.FLAG_ACTIVE

    @task
    async def _idle_task(self):
        if not self._state & ClientState.FLAG_CONNECTED or self._active or self._event(request.Event.IDLE, True):
            return
        _logger.debug("Going idle")
        request_ = request.RequestIdle(self.executor)
        request_.add_done_callback(self._unidle)
        event = request.Event.NONE
        for subsystem in await request_:
            event |= request.Event[subsystem.upper()]
        if event:
            self._event(event)

    def _event(self, event, one=False):
        for request_ in list(self._waiting_list):
            reply = request_._event_mask & event
            if reply:
                self._active = True
                request_.set_result(reply)
                if one:
                    return True
        return False


class StatusPropertyBase(object):
    def __init__(self, name, type_, default, on_set=None):
        super().__init__(type=type_)
        self._name = name
        self._type = type_
        self._default = default
        if isinstance(on_set, str):
            self._on_set = self._on_set_ampd
            self._ampd_command = on_set
        else:
            self._on_set = on_set

        self._orig_fset = self.fset
        self.fset = self._fset

    def _fset(self, instance, value):
        self._orig_fset(instance, value)
        if self._on_set is not None and not instance._block:
            self._on_set(instance, value)

    def _status_value(self, status):
        if self._name not in status:
            return self._default
        value = status[self._name]
        if self._type is not None:
            value = self._type(value)
        return value

    def _update(self, instance, status):
        value = self._status_value(status)
        if value != self.__get__(instance, instance.__class__):
            instance._block = True
            self.__set__(instance, value)
            instance._block = False

    @task
    async def _on_set_ampd(self, instance, value):
        await getattr(instance.ampd, self._ampd_command)(value)


def StatusProperty(base, *args, **kwargs):
    return type('StatusProperty', (StatusPropertyBase, base), {})(*args, **kwargs)


@task
async def _on_set_volume(instance, value):
    if instance._setting_volume:
        instance._setting_volume.cancel()
    task_ = instance._setting_volume = asyncio.current_task()
    value = instance.volume
    _logger.debug("Setting volume to {} at {}".format(value, task_))
    try:
        while True:
            try:
                await instance.ampd.setvol(value)
            except errors.ReplyError:
                await instance.ampd.idle(request.Event.PLAYER)
                continue
            status = await instance.ampd.status()
            if 'volume' not in status:
                return
            if int(status['volume']) == value:
                _logger.debug("Sucessfully set volume to {} at {}".format(value, task_))
                break
            await instance.ampd.idle(request.Event.PLAYER | request.Event.MIXER)
    finally:
        if instance._setting_volume == task_:
            instance._setting_volume = None


OPTION_NAMES = ['consume', 'random', 'repeat', 'single']

STATUS_PROPERTIES = [
    ('state', str, ''),
    ('bitrate', str, ''),
    ('updating_db', str, ''),
    ('partition', str, ''),
    ('elapsed', float, 0.0, 'seekcur'),
    ('duration', float, 0.0),
    ('volume', int, -1, _on_set_volume),
] + [
    (option, int, 0, option) for option in OPTION_NAMES
]


class PropertyPython(property):
    def __init__(self, type):
        super().__init__(self.fget, self.fset)

    def fget(self, instance, owner=None):
        return getattr(instance, '_server_property_' + self._name, self._default)

    def fset(self, instance, value):
        setattr(instance, '_server_property_' + self._name, value)


class ServerPropertiesBase(object):
    """
    Keeps track of various properties of the server:
    - status
    - current_song
    - state
    - volume
    - duration
    - elapsed
    - bitrate
    - consume, random, repeat, single
    - partition

    Assignment to volume, elapsed, consume, random, repeat, single is reflected in the server.

    Do not use this -- use ServerPropertiesGLib instead.
    """

    def __init__(self, executor):
        self.ampd = executor.sub_executor()
        self.ampd.set_callbacks(self._connect_cb, self._disconnect_cb)
        self._block = False
        self._setting_volume = None
        self._reset()

    def _reset(self):
        self.status = {}
        self._status_updated()
        self.current_song = {}

    @task
    async def _connect_cb(self):
        events = last_events = request.Event.PLAYER | request.Event.MIXER | request.Event.OPTIONS | request.Event.UPDATE
        while True:
            self.status = await self.ampd.status()
            self._status_updated()
            if self.state == 'stop':
                if self.current_song:
                    self.current_song = {}
            elif last_events | request.Event.PLAYER:
                new_current_song = await self.ampd.currentsong()
                if self.current_song != new_current_song:
                    self.current_song = new_current_song
            last_events = await self.ampd.idle(events, timeout=(int(self.elapsed + 1.5) - self.elapsed) if self.state == 'play' else 30)

    def _status_updated(self):
        for name, *args in STATUS_PROPERTIES:
            getattr(self.__class__, name)._update(self, self.status)

    def _disconnect_cb(self, reason, message):
        _logger.debug("Server properties disconnected.")
        self._reset()


properties = {name: StatusProperty(PropertyPython, name, *args) for name, *args in STATUS_PROPERTIES}
ServerProperties = type('ServerProperties', (ServerPropertiesBase,), dict(properties, __doc__=ServerPropertiesBase.__doc__))
