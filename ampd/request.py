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


import logging
import asyncio
import enum
import re

from . import errors


_logger = logging.getLogger(__name__.split('.')[0])


ERROR_RE = '^ACK \\[([0-9]+)@([0-9]+)\\] {([^}]*)} (.*)$'
SUCCESS = 'OK'
WELCOME_PREFIX = 'OK MPD '
LIST_SUCCESS = 'list_OK'
DELIM = ': '


def transform_empty(reply):
    reply = reply
    if reply == []:
        return True
    else:
        raise errors.ProtocolError(reply)


def transform_lists(reply):
    lists = {}
    for field, value in reply:
        lists.setdefault(field, []).append(value)
    return lists


def transform_subsets(reply, markers={'file': dict, 'directory': dict, 'playlist': dict}):
    subsets = []
    for item in reply:
        field = item[0]
        if field in markers:
            subset = []
            subsets.append((field, subset))
        subset.append(item)
    return transform_lists([(field, markers[field](subset)) for field, subset in subsets])


def transform_single_value(reply):
    if len(reply) == 1:
        return reply[0][1]
    else:
        raise errors.ProtocolError(reply)


def transform_single_list(reply):
    return [value for field, value in reply]


def transform_single_subset(reply):
    if not reply:
        return []
    marker = reply[0][0]
    return transform_subsets(reply, {marker: dict})[marker]


COMMANDS = {
    'add': transform_empty,
    'addid': transform_single_value,
    'addtagid': None,
    'channels': transform_single_list,
    'clear': transform_empty,
    'clearerror': transform_empty,
    'cleartagid': None,
    'close': lambda: None,
    'commands': transform_single_list,
    'config': dict,
    'consume': transform_empty,
    'count': dict,
    'crossfade': transform_empty,
    'currentsong': dict,
    'decoders': transform_lists,
    'delete': transform_empty,
    'deleteid': transform_empty,
    'delpartition': transform_empty,
    'disableoutput': transform_empty,
    'enableoutput': transform_empty,
    'find': transform_single_subset,
    'findadd': transform_empty,
    'kill': lambda: None,
    'list': transform_single_list,
    'listall': transform_lists,
    'listallinfo': transform_subsets,
    'listfiles': transform_subsets,
    'listmounts': transform_single_subset,
    'listneighbors': transform_single_subset,
    'listpartitions': transform_single_list,
    'listplaylist': transform_single_list,
    'listplaylistinfo': transform_single_subset,
    'listplaylists': transform_single_subset,
    'load': transform_empty,
    'lsinfo': transform_subsets,
    'mixrampdb': transform_empty,
    'mixrampdelay': transform_empty,
    'mount': None,
    'move': transform_empty,
    'moveid': transform_empty,
    'moveoutput': transform_empty,
    'next': transform_empty,
    'newpartition': transform_empty,
    'notcommands': transform_single_list,
    'outputs': transform_single_subset,
    'partition': transform_empty,
    'password': transform_empty,
    'pause': transform_empty,
    'ping': transform_empty,
    'play': transform_empty,
    'playid': transform_empty,
    'playlist': transform_single_list,
    'playlistadd': transform_empty,
    'playlistclear': transform_empty,
    'playlistdelete': transform_empty,
    'playlistfind': transform_single_subset,
    'playlistid': transform_single_subset,
    'playlistinfo': transform_single_subset,
    'playlistmove': transform_empty,
    'playlistsearch': transform_single_subset,
    'plchanges': transform_single_subset,
    'plchangesposid': transform_single_subset,
    'previous': transform_empty,
    'prio': transform_empty,
    'prioid': transform_empty,
    'random': transform_empty,
    'rangeid': transform_empty,
    'readcomments': dict,
    'readmessages': transform_single_subset,
    'rename': transform_empty,
    'repeat': transform_empty,
    'replay_gain_mode': transform_empty,
    'replay_gain_status': transform_single_value,
    'rescan': transform_single_value,
    'rm': transform_empty,
    'save': transform_empty,
    'search': transform_single_subset,
    'searchadd': transform_empty,
    'searchaddpl': transform_empty,
    'seek': transform_empty,
    'seekcur': transform_empty,
    'seekid': transform_empty,
    'sendmessage': transform_empty,
    'setvol': transform_empty,
    'shuffle': transform_empty,
    'single': transform_empty,
    'stats': dict,
    'status': dict,
    'sticker': None,
    'sticker_get': transform_single_value,
    'sticker_set': transform_empty,
    'sticker_delete': transform_empty,
    'sticker_list': transform_single_list,
    'sticker_find': transform_single_subset,
    'stop': transform_empty,
    'subscribe': transform_empty,
    'swap': transform_empty,
    'swapid': transform_empty,
    'tagtypes': transform_single_list,
    'toggleoutput': transform_empty,
    'unmount': None,
    'unsubscribe': transform_empty,
    'update': transform_single_value,
    'urlhandlers': transform_single_list,
    'volume': transform_empty,
}


class Request(asyncio.Future):
    def __init__(self, executor):
        super().__init__()
        self._executor = executor

    def set_result(self, result):
        _logger.debug("{} --> {}".format(self, result))
        if not self.done():
            super().set_result(result)

    def __await__(self):
        self._executor._log_request(self)
        return super().__await__()

    @staticmethod
    def _new_request(executor, name):
        if name in COMMANDS:
            return lambda *args: RequestCommand(executor, name, *args)
        elif name == 'idle':
            return lambda *args, **kwargs: RequestPassive(executor, *args, **kwargs)
        elif name == 'command_list':
            return lambda *arg: RequestCommandList(executor, *arg)
        elif name == '_raw':
            return lambda commandline: RequestCommandLine(executor, commandline, None)
        else:
            raise AttributeError(name)


class RequestActive(Request):
    def __init__(self, executor, commandline=None):
        super().__init__(executor)
        self._commandline = commandline

    def __repr__(self):
        return self._commandline

    def _process_reply(self, lines):
        parser = self._parser()
        parser.send(None)
        for line in lines:
            try:
                parser.send(line.decode('utf-8'))
            except StopIteration:
                return


class RequestWelcome(RequestActive):
    def __repr__(self):
        return 'WELCOME'

    def _parser(self):
        line = yield
        if self.cancelled():
            return
        elif line.startswith(WELCOME_PREFIX):
            self.set_result(line[len(WELCOME_PREFIX):])
        else:
            raise errors.ProtocolError(line)

    __await__ = asyncio.Future.__await__


class RequestCommandLine(RequestActive):
    def __init__(self, executor, commandline, transform):
        super().__init__(executor, commandline)
        self._transform = transform

    def _parser(self, success=SUCCESS):
        lines = []
        while True:
            line = yield
            match = re.match(ERROR_RE, line)
            if match:
                self.set_exception(errors.ReplyError(int(match.group(1)), int(match.group(2)), match.group(3), match.group(4), self._commandline))
                return
            elif line == success:
                self.set_result(lines if self._transform is None else self._transform(lines))
                return
            else:
                key, value = line.split(DELIM, 1)
                lines.append((key.replace('-', '_'), value))


class RequestCommand(RequestCommandLine):
    def __init__(self, executor, name, *args):
        args = ' '.join('"{}"'.format(arg.replace('"', '\\"')) if isinstance(arg, str) else str(arg) for arg in args)
        super().__init__(executor, name.replace('_', ' ') + ' ' + args, COMMANDS[name])


class RequestIdle(RequestCommandLine):
    def __init__(self, executor):
        super().__init__(executor, 'idle', transform_single_list)


class RequestCommandList(RequestActive):
    def __init__(self, executor, *commands):
        if len(commands) == 1:
            self._commands = tuple(commands[0])
        else:
            self._commands = tuple(commands)
        if not all(isinstance(command, RequestCommand) for command in self._commands):
            raise errors.CommandError
        super().__init__(executor, '\n'.join(['command_list_ok_begin'] + [command._commandline for command in self._commands] + ['command_list_end']))

    def _parser(self):
        results = []
        for command in self._commands:
            yield from command._parser(LIST_SUCCESS)
            if command.exception() is not None:
                self.set_exception(command.exception())
                return
            results.append(command.result())
        line = yield
        if line != SUCCESS:
            raise errors.ProtocolError("Too many replies for command list")
        if not self.cancelled():
            self.set_result(results)


EVENTS = {name: 1 << i for i, name in enumerate(['DATABASE',
                                                 'UPDATE',
                                                 'STORED_PLAYLIST',
                                                 'PLAYLIST',
                                                 'PLAYER',
                                                 'MIXER',
                                                 'OUTPUT',
                                                 'OPTIONS',
                                                 'PARTITION',
                                                 'STICKER',
                                                 'SUBSCRIPTION',
                                                 'MESSAGE',
                                                 'CONNECT',
                                                 'IDLE',
                                                 'TIMEOUT'])}
EVENTS['NONE'] = 0
EVENTS['ANY'] = EVENTS['CONNECT'] - 1
Event = enum.IntFlag('Event', EVENTS)
del EVENTS

Event.__doc__ = """
An enumeration of possible events for the idle request.
Possible values are:
- An MPD SUBSYSTEM name (in uppercase).
- ANY - match any subsystem.
- CONNECT - client is connected to server.
- IDLE - client is idle.
- TIMEOUT - flagged in the return value if a timeout ocurred.
"""


class RequestPassive(Request):
    """
    Emulates MPD's 'idle' command, with some improvements.

    Returns as soon as one of the conditions is satisfied, with a list of the satisfied conditions:

        reply = await executor.idle(event_mask, timeout=None)

    See the Event enumeration for possible events.
    The timeout is given in seconds.
    """

    def __init__(self, executor, event_mask, *, timeout=None):
        super().__init__(executor)
        self._event_mask = event_mask
        self._timeout = timeout

    def __await__(self):
        if self._timeout is not None:
            self._timeout_handle = asyncio.get_event_loop().call_later(self._timeout, lambda: self.set_result(Event.TIMEOUT))
            self._event_mask |= Event.TIMEOUT
            self.add_done_callback(self._cancel_timeout)
        return super().__await__()

    @staticmethod
    def _cancel_timeout(self):
        self._timeout_handle.cancel()

    def __repr__(self):
        return 'IDLE({})'.format(' | '.join(name for name, event in Event.__members__.items() if event & self._event_mask and name != 'ANY'))
