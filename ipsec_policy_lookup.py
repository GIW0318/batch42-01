"""IPsec policy/SA/selector synchronized lookup table (skeleton)."""


class IpsecError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class IpsecPolicyTable:
    def __init__(self):
        pass

    def add_policy(self, selector, action="encrypt", priority=100,
                   proposal=None, direction="out"):
        raise NotImplementedError

    def add_policy_pair(self, selector, action="encrypt", priority=100, proposal=None):
        raise NotImplementedError

    def validate_bidirectional(self, policy_id):
        raise NotImplementedError

    def delete_policy(self, policy_id):
        raise NotImplementedError

    def lookup(self, src, dst, proto="tcp", sport=0, dport=0, direction="out"):
        raise NotImplementedError

    def install_sa(self, policy_id, spi, enc="aes256", dh_group=14, port=500):
        raise NotImplementedError

    def update_proposal(self, policy_id, proposal):
        raise NotImplementedError

    def select_sa(self, policy_id):
        raise NotImplementedError

    def sa_for_port(self, policy_id, port):
        raise NotImplementedError

    def list_sa_spis(self, policy_id):
        raise NotImplementedError

    def expire_sa(self, spi):
        raise NotImplementedError

    def check_replay(self, spi, seq):
        raise NotImplementedError

    def rekey(self, policy_id, new_spi, enc="aes256"):
        raise NotImplementedError

    def policy_count(self):
        raise NotImplementedError

    def sa_count(self):
        raise NotImplementedError

    def health(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
