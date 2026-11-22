"""Login queue positions, one-shot tokens and shard recovery.

Positions are the live 1-based rank inside a shard queue and may be
recomputed as the queue changes.  A persistent monotonic cursor orders
entries and survives restarts via snapshot/restore, so sequence numbers
are never reused or reassigned.  Tokens are single use; replay is
rejected and logged, and expiry returns the account to its original
position.  Kicked accounts keep their position for ``position_hold``
seconds before the slot is released.
"""

import copy

ALREADY_QUEUED = "already_queued"
NOT_AT_FRONT = "not_at_front"
TOKEN_REPLAYED = "token_replayed"
TOKEN_EXPIRED = "token_expired"
NOT_QUEUED = "not_queued"
UNKNOWN_TOKEN = "unknown_token"
ACCOUNT_KICKED = "account_kicked"

DEFAULT_SERVICE_SECONDS = 5.0
MIN_SERVICE_SECONDS = 1.0
MAX_SERVICE_SECONDS = 300.0


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
        self._day = 0
        self._cursor = 0
        self._token_counter = 0
        self._queues = {}
        self._accounts = {}
        self._holds = {}
        self._tokens = {}
        self._entered = {}
        self._capacities = {}
        self._replay_log = []
        self._admissions = 0

    def advance(self, seconds):
        self.server_time += seconds
        self._purge_holds()

    def set_day(self, day):
        self._day = day

    def enqueue(self, account_id, shard_id="s1", vip=False):
        self._purge_holds()
        existing = self._accounts.get(account_id)
        if existing is not None:
            if existing["shard"] != shard_id:
                raise QueueError(
                    ALREADY_QUEUED,
                    "account %s already queued on shard %s"
                    % (account_id, existing["shard"]))
            result = {"status": ALREADY_QUEUED, "account": account_id,
                      "shard": shard_id}
            if existing["state"] == "queued":
                result["position"] = self.position(account_id)
            return result
        hold = self._holds.pop(account_id, None)
        queue = self._queues.setdefault(shard_id, [])
        self._cursor += 1
        entry = {"account": account_id, "shard": shard_id, "vip": vip,
                 "seq": self._cursor}
        if hold is not None and hold["shard"] == shard_id:
            index = min(max(hold["rank"] - 1, 0), len(queue))
            queue.insert(index, entry)
        else:
            queue.append(entry)
            index = len(queue) - 1
        self._accounts[account_id] = {"shard": shard_id, "state": "queued"}
        return {"status": "queued", "account": account_id, "shard": shard_id,
                "position": index + 1}

    def issue_token(self, account_id, shard_id="s1"):
        self._purge_holds()
        info = self._accounts.get(account_id)
        if info is None:
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        if info["state"] == "token":
            record = self._token_for_account(account_id)
            if record is not None and not self._token_expired(record):
                return {"status": ALREADY_QUEUED, "account": account_id,
                        "shard": info["shard"], "token": record["token"]}
            if record is not None:
                self._expire_token(record)
            info = self._accounts[account_id]
        if info["state"] != "queued":
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        if info["shard"] != shard_id:
            raise QueueError(NOT_QUEUED, "account %s is queued on shard %s"
                             % (account_id, info["shard"]))
        queue = self._queues.get(shard_id, [])
        if not queue or queue[0]["account"] != account_id:
            raise QueueError(NOT_AT_FRONT, "account %s is not at the front"
                             % account_id)
        entry = queue.pop(0)
        self._token_counter += 1
        token = "tok-%d" % self._token_counter
        self._tokens[token] = {
            "token": token,
            "account": account_id,
            "shard": shard_id,
            "vip": entry["vip"],
            "rank": 1,
            "issued_at": self.server_time,
            "expires_at": self.server_time + self.token_ttl,
            "consumed": False,
            "expired": False,
        }
        info["state"] = "token"
        return {"status": "issued", "account": account_id, "shard": shard_id,
                "token": token,
                "expires_at": self.server_time + self.token_ttl}

    def consume_token(self, token):
        self._purge_holds()
        record = self._tokens.get(token)
        if record is None:
            raise QueueError(UNKNOWN_TOKEN, "unknown token")
        if record["consumed"]:
            self._replay_log.append({
                "token": token,
                "account": record["account"],
                "shard": record["shard"],
                "server_time": self.server_time,
            })
            raise QueueError(TOKEN_REPLAYED, "token already used")
        if record["expired"] or self._token_expired(record):
            if not record["expired"]:
                self._expire_token(record)
            raise QueueError(TOKEN_EXPIRED, "token expired")
        record["consumed"] = True
        account_id = record["account"]
        self._accounts[account_id] = {"shard": record["shard"],
                                      "state": "entered"}
        self._entered[account_id] = {
            "shard": record["shard"],
            "entered_at": self.server_time,
            "last_heartbeat": self.server_time,
        }
        self._admissions += 1
        return {"status": "entered", "account": account_id,
                "shard": record["shard"]}

    def heartbeat(self, account_id):
        info = self._accounts.get(account_id)
        if info is None:
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        if info["state"] == "entered":
            self._entered[account_id]["last_heartbeat"] = self.server_time
            return {"status": "online", "account": account_id}
        return {"status": info["state"], "account": account_id}

    def position(self, account_id):
        info = self._accounts.get(account_id)
        if info is None or info["state"] != "queued":
            if account_id in self._holds:
                raise QueueError(ACCOUNT_KICKED,
                                 "account %s was kicked" % account_id)
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        queue = self._queues.get(info["shard"], [])
        for index, entry in enumerate(queue):
            if entry["account"] == account_id:
                return index + 1
        raise QueueError(NOT_QUEUED, "account %s is not queued" % account_id)

    def resize(self, shard_id, capacity):
        self._capacities[shard_id] = capacity
        return {"shard": shard_id, "capacity": capacity}

    def kick(self, account_id):
        self._purge_holds()
        info = self._accounts.get(account_id)
        if info is None or info["state"] != "queued":
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        shard_id = info["shard"]
        queue = self._queues.get(shard_id, [])
        rank = None
        for index, entry in enumerate(queue):
            if entry["account"] == account_id:
                rank = index + 1
                del queue[index]
                break
        if rank is None:
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        self._holds[account_id] = {"shard": shard_id, "rank": rank,
                                   "kicked_at": self.server_time}
        del self._accounts[account_id]
        return {"status": "kicked", "account": account_id,
                "shard": shard_id, "position": rank}

    def upgrade_vip(self, account_id):
        info = self._accounts.get(account_id)
        if info is None or info["state"] != "queued":
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        queue = self._queues.get(info["shard"], [])
        entry = None
        for index, candidate in enumerate(queue):
            if candidate["account"] == account_id:
                entry = candidate
                del queue[index]
                break
        if entry is None:
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        entry["vip"] = True
        index = 0
        for position, candidate in enumerate(queue):
            if candidate["vip"]:
                index = position + 1
        queue.insert(index, entry)
        return {"status": "upgraded", "account": account_id,
                "shard": info["shard"], "position": index + 1}

    def queue_length(self, shard_id="s1"):
        self._purge_holds()
        return len(self._queues.get(shard_id, []))

    def online_count(self):
        return len(self._entered)

    def stats(self, shard_id="s1"):
        self._purge_holds()
        entered = sum(1 for record in self._entered.values()
                      if record["shard"] == shard_id)
        return {
            "shard": shard_id,
            "queued": len(self._queues.get(shard_id, [])),
            "entered": entered,
            "capacity": self._capacities.get(shard_id, self.capacity),
        }

    def estimated_wait(self, account_id):
        info = self._accounts.get(account_id)
        if info is None:
            raise QueueError(NOT_QUEUED, "account %s is not queued"
                             % account_id)
        if info["state"] != "queued":
            return 0
        rank = self.position(account_id)
        if self._admissions > 0 and self.server_time > 0:
            average = float(self.server_time) / self._admissions
            average = max(MIN_SERVICE_SECONDS,
                          min(MAX_SERVICE_SECONDS, average))
        else:
            average = DEFAULT_SERVICE_SECONDS
        return max(0.0, (rank - 1) * average)

    def replay_log(self):
        return list(self._replay_log)

    def snapshot(self):
        return {
            "capacity": self.capacity,
            "server_time": self.server_time,
            "token_ttl": self.token_ttl,
            "position_hold": self.position_hold,
            "day": self._day,
            "cursor": self._cursor,
            "token_counter": self._token_counter,
            "queues": copy.deepcopy(self._queues),
            "accounts": copy.deepcopy(self._accounts),
            "holds": copy.deepcopy(self._holds),
            "tokens": copy.deepcopy(self._tokens),
            "entered": copy.deepcopy(self._entered),
            "capacities": dict(self._capacities),
            "replay_log": copy.deepcopy(self._replay_log),
            "admissions": self._admissions,
        }

    def restore(self, snapshot):
        self.capacity = snapshot.get("capacity", self.capacity)
        self.server_time = snapshot.get("server_time", self.server_time)
        self.token_ttl = snapshot.get("token_ttl", self.token_ttl)
        self.position_hold = snapshot.get("position_hold",
                                          self.position_hold)
        self._day = snapshot.get("day", 0)
        self._cursor = snapshot.get("cursor", 0)
        self._token_counter = snapshot.get("token_counter", 0)
        self._queues = copy.deepcopy(snapshot.get("queues", {}))
        self._accounts = copy.deepcopy(snapshot.get("accounts", {}))
        self._holds = copy.deepcopy(snapshot.get("holds", {}))
        self._tokens = copy.deepcopy(snapshot.get("tokens", {}))
        self._entered = copy.deepcopy(snapshot.get("entered", {}))
        self._capacities = dict(snapshot.get("capacities", {}))
        self._replay_log = copy.deepcopy(snapshot.get("replay_log", []))
        self._admissions = snapshot.get("admissions", 0)

    def _purge_holds(self):
        expired = [account_id for account_id, hold in self._holds.items()
                   if self.server_time - hold["kicked_at"]
                   > self.position_hold]
        for account_id in expired:
            del self._holds[account_id]

    def _token_for_account(self, account_id):
        for record in self._tokens.values():
            if record["account"] == account_id and not record["consumed"]:
                return record
        return None

    def _token_expired(self, record):
        return self.server_time > record["expires_at"]

    def _expire_token(self, record):
        record["expired"] = True
        account_id = record["account"]
        shard_id = record["shard"]
        queue = self._queues.setdefault(shard_id, [])
        index = min(max(record["rank"] - 1, 0), len(queue))
        self._cursor += 1
        queue.insert(index, {"account": account_id, "shard": shard_id,
                             "vip": record["vip"], "seq": self._cursor})
        self._accounts[account_id] = {"shard": shard_id, "state": "queued"}
