class ChatError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


class ChannelFanout:
    def __init__(self, max_length=512):
        self.max_length = max_length
        self._channels = {}
        self._connections = {}
        self._subscriptions = {}
        self._messages = {}
        self._requests = set()
        self._muted = {}
        self._blocks = {}
        self._names = {}
        self._filter = []
        self._last_delivered = {}

    def create_channel(self, channel_id, kind="public", min_level=0):
        raise NotImplementedError()

    def connect(self, conn_id, account_id, server="s1", level=0):
        raise NotImplementedError()

    def subscribe(self, conn_id, channel_id):
        raise NotImplementedError()

    def unsubscribe(self, conn_id, channel_id):
        raise NotImplementedError()

    def switch(self, conn_id, channel_id):
        raise NotImplementedError()

    def send(self, request_id, conn_id, channel_id, text, level=None):
        raise NotImplementedError()

    def ingest(self, source, seq, channel_id, text, author_id,
               request_id=None):
        raise NotImplementedError()

    def deliver(self, conn_id):
        raise NotImplementedError()

    def backfill(self, conn_id, channel_id, since_seq):
        raise NotImplementedError()

    def mute(self, account_id, until_ms):
        raise NotImplementedError()

    def block(self, account_id, blocked_id):
        raise NotImplementedError()

    def rename(self, account_id, new_name):
        raise NotImplementedError()

    def name_of(self, account_id):
        raise NotImplementedError()

    def set_filter(self, words):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
