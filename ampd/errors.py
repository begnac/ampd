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


class ConnectionError(Exception):
    pass


class ReplyError(Exception):
    def __init__(self, error, command_list_num, command, message, commandline):
        self.error = error
        self.command_list_num = command_list_num
        self.command = command
        self.message = message
        self.commandline = commandline

    def __str__(self):
        return "{}: {} (error {}) for command no. {} ({}) : {}".format(self.__class__.__name__, self.message, self.error, self.command_list_num, self.command, self.commandline)


class ProtocolError(Exception):
    pass


class CommandError(Exception):
    pass


class DeprecationWarning(Warning):
    pass
