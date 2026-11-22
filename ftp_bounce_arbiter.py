"""FTP control/data connection bounce arbiter skeleton."""

import os
import threading

BOUNCE = "bounce"
DATA_SOURCE_MISMATCH = "data-source-mismatch"
NO_PASSIVE_PORT = "no-passive-port"
INVALID_REST = "invalid-rest"
PATH_TRAVERSAL = "path-traversal"
PERMISSION_DENIED = "permission-denied"
DATA_IN_USE = "data-in-use"
UNKNOWN_CONNECTION = "unknown-connection"


class FtpError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class FtpBounceArbiter:
    """Binds control and data connections by 5-tuple.

    PORT targets must equal the control connection peer, passive ports are
    reserved per control connection and released atomically, and data
    connections are accepted only from the control peer address.
    """

    def __init__(self, passive_ports=None, advertised_host="203.0.113.10",
                 passive_timeout=30, root="/srv/ftp",
                 system_dirs=("/etc", "/proc", "/sys")):
        self._free_ports = list(passive_ports or [])
        self.advertised_host = advertised_host
        self.passive_timeout = passive_timeout
        self.root = os.path.normpath(root)
        self.system_dirs = tuple(system_dirs)
        self._clock = 0
        self._lock = threading.RLock()
        self._controls = {}
        self._reserved = {}
        self._data = {}

    def open_control(self, conn_id, peer_ip, peer_port=0):
        with self._lock:
            self._controls[conn_id] = {
                "peer_ip": peer_ip,
                "peer_port": peer_port,
                "initiator": None,
                "port_target": None,
            }

    def close_control(self, conn_id):
        with self._lock:
            self._controls.pop(conn_id, None)
            self._data.pop(conn_id, None)
            released = self._reserved.pop(conn_id, [])
            self._free_ports.extend(released)
            return released

    def _control(self, conn_id):
        control = self._controls.get(conn_id)
        if control is None:
            raise FtpError(UNKNOWN_CONNECTION, conn_id)
        return control

    def handle_port(self, conn_id, host, port):
        with self._lock:
            control = self._control(conn_id)
            if host != control["peer_ip"]:
                raise FtpError(BOUNCE,
                               "PORT target %s does not match control peer %s"
                               % (host, control["peer_ip"]))
            control["initiator"] = "server"
            control["port_target"] = (host, port)
            return {"host": host, "port": port}

    def handle_pasv(self, conn_id):
        port = self.allocate_passive_port(conn_id)
        return {"host": self.advertised_host, "port": port,
                "initiator": "client"}

    def handle_epsv(self, conn_id):
        port = self.allocate_passive_port(conn_id)
        return {"port": port, "initiator": "client"}

    def initiator(self, conn_id):
        with self._lock:
            return self._control(conn_id)["initiator"]

    def allocate_passive_port(self, conn_id):
        with self._lock:
            control = self._control(conn_id)
            previous = self._reserved.get(conn_id, [])
            if previous:
                return previous[0]
            if not self._free_ports:
                raise FtpError(NO_PASSIVE_PORT,
                               "passive port pool exhausted")
            port = self._free_ports.pop(0)
            self._reserved[conn_id] = [port]
            control["initiator"] = "client"
            return port

    def release_passive_port(self, conn_id, port):
        with self._lock:
            ports = self._reserved.get(conn_id, [])
            if port in ports:
                ports.remove(port)
                if not ports:
                    self._reserved.pop(conn_id, None)
                self._free_ports.append(port)
            return port

    def reserved_ports(self, conn_id=None):
        with self._lock:
            if conn_id is not None:
                return list(self._reserved.get(conn_id, []))
            return [port for ports in self._reserved.values() for port in ports]

    def open_data(self, conn_id, source_ip, source_port=0):
        with self._lock:
            control = self._control(conn_id)
            if source_ip != control["peer_ip"]:
                raise FtpError(DATA_SOURCE_MISMATCH,
                               "data source %s does not match control peer %s"
                               % (source_ip, control["peer_ip"]))
            self._data[conn_id] = {
                "source_ip": source_ip,
                "source_port": source_port,
                "opened_at": self._clock,
            }
            return {"connected": True}

    def close_data(self, conn_id):
        with self._lock:
            self._data.pop(conn_id, None)

    def active_data_connections(self):
        with self._lock:
            return len(self._data)

    def advance(self, seconds):
        with self._lock:
            self._clock += seconds
            return self._clock

    def expire_data(self, now=None):
        with self._lock:
            if now is None:
                now = self._clock
            expired = [conn_id for conn_id, data in self._data.items()
                       if now - data["opened_at"] >= self.passive_timeout]
            for conn_id in expired:
                del self._data[conn_id]
            return expired

    def validate_rest(self, conn_id, offset, file_size):
        self._control(conn_id)
        if offset < 0 or offset > file_size:
            raise FtpError(INVALID_REST,
                           "offset %d outside file size %d"
                           % (offset, file_size))
        return offset

    def _user_base(self, user):
        if user == "anonymous":
            return os.path.join(self.root, "pub")
        return os.path.join(self.root, user)

    def resolve_stor_path(self, conn_id, user, filename):
        self._control(conn_id)
        base = self._user_base(user)
        candidate = os.path.normpath(os.path.join(base, filename))
        if candidate != base and not candidate.startswith(base + os.sep):
            raise FtpError(PATH_TRAVERSAL,
                           "%r escapes user root" % (filename,))
        return candidate

    def allow_write(self, conn_id, user, path):
        self._control(conn_id)
        candidate = os.path.normpath(path)
        for system_dir in self.system_dirs:
            system_dir = os.path.normpath(system_dir)
            if candidate == system_dir or candidate.startswith(system_dir + os.sep):
                return False
        base = self._user_base(user)
        if candidate != base and not candidate.startswith(base + os.sep):
            return False
        return True
