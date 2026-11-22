"""Login queue positions, one-shot tokens and shard recovery (skeleton).

A position is issued once from a persistent server cursor, tokens are single
use and expiry returns the account to its held position.  No behaviour is
implemented yet.
"""

ALREADY_QUEUED = "already_queued"
NOT_AT_FRONT = "not_at_front"
TOKEN_REPLAYED = "token_replayed"
TOKEN_EXPIRED = "token_expired"
NOT_QUEUED = "not_queued"
UNKNOWN_TOKEN = "unknown_token"
ACCOUNT_KICKED = "account_kicked"


class QueueError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class LoginQueueShard:
    def __init__(self, capacity=100, server_time=0, token_ttl=60,
                 position_hold=120):
        self.capacity = capacity
        self.server_time = server_time
        self.token_ttl = token_ttl
        self.position_hold = position_hold

    def advance(self, seconds):
        raise NotImplementedError

    def set_day(self, day):
        raise NotImplementedError

    def enqueue(self, account_id, shard_id="s1", vip=False):
        raise NotImplementedError

    def issue_token(self, account_id, shard_id="s1"):
        raise NotImplementedError

    def consume_token(self, token):
        raise NotImplementedError

    def heartbeat(self, account_id):
        raise NotImplementedError

    def position(self, account_id):
        raise NotImplementedError

    def resize(self, shard_id, capacity):
        raise NotImplementedError

    def kick(self, account_id):
        raise NotImplementedError

    def upgrade_vip(self, account_id):
        raise NotImplementedError

    def queue_length(self, shard_id="s1"):
        raise NotImplementedError

    def online_count(self):
        raise NotImplementedError

    def stats(self, shard_id="s1"):
        raise NotImplementedError

    def estimated_wait(self, account_id):
        raise NotImplementedError

    def replay_log(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
