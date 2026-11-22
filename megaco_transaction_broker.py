"""H.248/Megaco transaction broker skeleton."""

UNKNOWN_CONTEXT = "unknown-context"
INVALID_ACTION = "invalid-action"
TX_BUSY = "tx-busy"
ENCODING_MISMATCH = "encoding-mismatch"
TERMINATION_LIMIT = "termination-limit"
UNKNOWN_TERMINATION = "unknown-termination"


class MegacoError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class MegacoTransactionBroker:
    """Transaction-id idempotency over Context/Termination state.

    A transaction's Actions apply all together or not at all.  Termination
    ids are normalized to lower case with numeric prefixes stripped of
    leading zeros.  Encoding is decided from the message header.
    """

    def __init__(self, max_terminations=1000):
        self.max_terminations = max_terminations

    def create_context(self, context_id=None):
        raise NotImplementedError

    def delete_context(self, context_id):
        raise NotImplementedError

    def normalize_termination_id(self, termination_id):
        raise NotImplementedError

    def add_termination(self, context_id, termination_id, emergency=False):
        raise NotImplementedError

    def termination_info(self, context_id, termination_id):
        raise NotImplementedError

    def transaction(self, tx_id, actions, encoding="text"):
        raise NotImplementedError

    def begin_transaction(self, tx_id):
        raise NotImplementedError

    def end_transaction(self, tx_id):
        raise NotImplementedError

    def audit_value(self, context_id):
        raise NotImplementedError

    def keepalive(self, context_id):
        raise NotImplementedError

    def sweep_empty(self):
        raise NotImplementedError

    def decode_transaction(self, header, payload):
        raise NotImplementedError

    def context_count(self):
        raise NotImplementedError

    def termination_count(self, context_id):
        raise NotImplementedError

    def play_count(self, termination_id):
        raise NotImplementedError

    def re_register(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
