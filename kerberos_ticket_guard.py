"""Kerberos ticket realm/principal/kvno/replay/PAC validation (skeleton)."""


class KerberosError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class KerberosTicketGuard:
    def __init__(self, clock_skew=300, replay_ttl=300):
        self.clock_skew = clock_skew
        self.replay_ttl = replay_ttl

    def add_realm(self, realm, current_kvno=1):
        raise NotImplementedError

    def rotate_keytab(self, principal, new_kvno, *, now=0.0, overlap=300.0):
        raise NotImplementedError

    def authorize_delegation(self, principal, target_service):
        raise NotImplementedError

    def authenticate(self, ticket, client_addr, *, now=0.0, service=None):
        raise NotImplementedError

    def delegate(self, ticket, client_addr, target_service, *, now=0.0):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
