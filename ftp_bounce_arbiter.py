"""FTP control/data connection bounce arbiter skeleton."""

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
        self.passive_ports = list(passive_ports or [])
        self.advertised_host = advertised_host
        self.passive_timeout = passive_timeout
        self.root = root
        self.system_dirs = tuple(system_dirs)
        self._clock = 0

    def open_control(self, conn_id, peer_ip, peer_port=0):
        raise NotImplementedError

    def close_control(self, conn_id):
        raise NotImplementedError

    def handle_port(self, conn_id, host, port):
        raise NotImplementedError

    def handle_pasv(self, conn_id):
        raise NotImplementedError

    def handle_epsv(self, conn_id):
        raise NotImplementedError

    def initiator(self, conn_id):
        raise NotImplementedError

    def allocate_passive_port(self, conn_id):
        raise NotImplementedError

    def release_passive_port(self, conn_id, port):
        raise NotImplementedError

    def reserved_ports(self, conn_id=None):
        raise NotImplementedError

    def open_data(self, conn_id, source_ip, source_port=0):
        raise NotImplementedError

    def close_data(self, conn_id):
        raise NotImplementedError

    def active_data_connections(self):
        raise NotImplementedError

    def advance(self, seconds):
        raise NotImplementedError

    def expire_data(self, now=None):
        raise NotImplementedError

    def validate_rest(self, conn_id, offset, file_size):
        raise NotImplementedError

    def resolve_stor_path(self, conn_id, user, filename):
        raise NotImplementedError

    def allow_write(self, conn_id, user, path):
        raise NotImplementedError
