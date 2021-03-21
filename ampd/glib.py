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


from gi.repository import GObject

from . import client


class ClientGLib(client.Client, GObject.Object):
    """
    Adds GLib scheduling and signal functionality to Client.

    GLib signals:
      client-connected
      client-disconnected(reason)
    """

    __gsignals__ = {
        'client-connected': (GObject.SIGNAL_RUN_FIRST, None, ()),
        'client-disconnected': (GObject.SIGNAL_RUN_FIRST, None, (int, str)),
    }

    def __init__(self, *, excepthook=None):
        GObject.Object.__init__(self)
        super().__init__(excepthook=excepthook)
        self.executor.set_callbacks(self._connect_cb, self._disconnect_cb)

    def _connect_cb(self):
        self.emit('client-connected')

    def _disconnect_cb(self, reason, message):
        self.emit('client-disconnected', reason, message)


class ServerPropertiesGLibBase(client.ServerPropertiesBase, GObject.Object):
    """
    Adds GLib property and signal functionality to ServerProperties.

    GLib signals:
      server-error(message)
    """

    current_song = GObject.Property()
    status = GObject.Property()

    __gsignals__ = {
        'server-error': (GObject.SIGNAL_RUN_FIRST, None, (str,)),
    }

    def __init__(self, client):
        GObject.Object.__init__(self)
        super(ServerPropertiesGLibBase, self).__init__(client)

    def _status_updated(self):
        super()._status_updated()
        if 'error' in self.status:
            self.emit('server-error', self.status['error'])
            client.task(self.ampd.clearerror)()


properties = {name: client.StatusProperty(GObject.Property, name, *args) for name, *args in client.STATUS_PROPERTIES}
ServerPropertiesGLib = type('ServerPropertiesGLib', (ServerPropertiesGLibBase,), dict(properties, __doc__=ServerPropertiesGLibBase.__doc__))
