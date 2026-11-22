"""Roguelike run seed, random stream, floor snapshot and settlement.

One run owns one deterministic random stream, chest rewards are idempotent,
recovery returns to the last committed floor and daily settlement happens
once per account and server day.
"""

import copy
import hashlib

UNKNOWN_RUN = "unknown_run"
ALREADY_LOOTED = "already_looted"
ALREADY_JOINED = "already_joined"
ALREADY_IN_RUN = "already_in_run"
ALREADY_SETTLED = "already_settled"
ITEM_CONSUMED = "item_consumed"
CONTENT_MISMATCH = "content_mismatch"


def _hash_int(*parts):
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


class RunError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class RoguelikeRunSeed:
    def __init__(self, server_day=1, seed_base=90210, content_version="v1"):
        self.server_day = server_day
        self.seed_base = seed_base
        self.content_version = content_version
        self._runs = {}
        self._player_runs = {}
        self._settlements = {}
        self._leaderboard = []

    def _get_run(self, run_id):
        run = self._runs.get(run_id)
        if run is None:
            raise RunError(UNKNOWN_RUN, run_id)
        return run

    def advance(self, seconds):
        # Frame ticks advance wall time only; the random stream is consumed
        # exclusively through next_random so replays stay deterministic.
        return None

    def set_server_day(self, day):
        self.server_day = day

    def start_run(self, run_id, account_id, seed=None, mode="normal"):
        if seed is None:
            seed = self.daily_seed(account_id) if mode == "daily" else _hash_int(
                self.seed_base, run_id, account_id)
        run = {
            "run_id": run_id,
            "account_id": account_id,
            "seed": seed,
            "mode": mode,
            "start_day": self.server_day,
            "content_version": self.content_version,
            "players": [account_id],
            "counters": {},
            "chests": {},
            "inventory": {account_id: []},
            "pending": {account_id: []},
            "committed_floor": None,
            "revive_count": 0,
        }
        self._runs[run_id] = run
        self._player_runs[account_id] = run_id
        return {"run_id": run_id, "seed": seed, "mode": mode,
                "status": "started"}

    def run_state(self, run_id):
        run = self._get_run(run_id)
        return {
            "run_id": run_id,
            "account_id": run["account_id"],
            "mode": run["mode"],
            "start_day": run["start_day"],
            "players": list(run["players"]),
            "floor": (run["committed_floor"] or {}).get("floor", 0),
        }

    def daily_seed(self, account_id, client_day=None):
        # The daily seed is derived from the server day only; the client
        # supplied day is ignored so timezone shifts cannot grant reruns.
        return _hash_int(self.seed_base, "daily", self.server_day, account_id)

    def next_random(self, run_id, stream="shared"):
        run = self._get_run(run_id)
        index = run["counters"].get(stream, 0)
        run["counters"][stream] = index + 1
        return _hash_int(run["seed"], stream, index)

    def open_chest(self, run_id, floor, chest_id):
        run = self._get_run(run_id)
        key = (floor, chest_id)
        if key in run["chests"]:
            return {"status": ALREADY_LOOTED, "floor": floor,
                    "chest_id": chest_id,
                    "items": list(run["chests"][key])}
        roll = self.next_random(run_id)
        items = ["item-%d" % (roll % 1000)]
        run["chests"][key] = list(items)
        owner = run["account_id"]
        run["inventory"].setdefault(owner, []).extend(items)
        return {"status": "looted", "floor": floor, "chest_id": chest_id,
                "items": items}

    def grant_item(self, run_id, item_id, player_id=None, count=1):
        run = self._get_run(run_id)
        player_id = player_id or run["account_id"]
        run["inventory"].setdefault(player_id, []).extend(
            [item_id] * max(0, count))
        return {"status": "granted", "item_id": item_id,
                "player_id": player_id, "count": max(0, count)}

    def pick_up(self, run_id, item_id, player_id=None):
        run = self._get_run(run_id)
        player_id = player_id or run["account_id"]
        run["pending"].setdefault(player_id, []).append(item_id)
        return {"status": "picked_up", "item_id": item_id,
                "player_id": player_id}

    def commit_floor(self, run_id, floor, state):
        run = self._get_run(run_id)
        committed = dict(state)
        committed["floor"] = floor
        committed["content_version"] = run["content_version"]
        run["committed_floor"] = committed
        for player_id, items in run["pending"].items():
            run["inventory"].setdefault(player_id, []).extend(items)
        run["pending"] = {}
        return {"status": "committed", "floor": floor}

    def recover(self, run_id):
        run = self._get_run(run_id)
        committed = run["committed_floor"]
        if committed is None:
            raise RunError(UNKNOWN_RUN, "no committed floor for %s" % run_id)
        if committed.get("content_version") != self.content_version:
            raise RunError(CONTENT_MISMATCH,
                           "snapshot %s != client %s" % (
                               committed.get("content_version"),
                               self.content_version))
        # Uncommitted pickups are dropped, never reissued.
        run["pending"] = {}
        return dict(committed)

    def join_run(self, run_id, player_id):
        run = self._get_run(run_id)
        if player_id in run["players"]:
            return {"status": ALREADY_JOINED, "run_id": run_id,
                    "player_id": player_id}
        if player_id in self._player_runs:
            raise RunError(ALREADY_IN_RUN, player_id)
        run["players"].append(player_id)
        run["inventory"].setdefault(player_id, [])
        self._player_runs[player_id] = run_id
        return {"status": "joined", "run_id": run_id,
                "player_id": player_id}

    def party_relics(self, run_id, player_id):
        run = self._get_run(run_id)
        committed = run["committed_floor"] or {}
        return list(committed.get("relics", []))

    def settle_daily(self, account_id, run_id):
        run = self._get_run(run_id)
        key = (account_id, run["start_day"])
        if key in self._settlements:
            original = self._settlements[key]
            return {"status": ALREADY_SETTLED, "account_id": account_id,
                    "run_id": original["run_id"], "score": original["score"]}
        score = _hash_int(run["seed"], "settle", run_id)
        entry = {"account_id": account_id, "run_id": run_id,
                 "score": score, "day": run["start_day"]}
        self._settlements[key] = entry
        self._leaderboard.append(entry)
        return {"status": "settled", "account_id": account_id,
                "run_id": run_id, "score": score}

    def use_revive(self, run_id, item_id, player_id):
        run = self._get_run(run_id)
        items = run["inventory"].get(player_id, [])
        if item_id not in items:
            raise RunError(ITEM_CONSUMED,
                           "%s has no %s" % (player_id, item_id))
        items.remove(item_id)
        run["revive_count"] += 1
        return {"status": "used", "item_id": item_id,
                "player_id": player_id}

    def revive_count(self, run_id):
        return self._get_run(run_id)["revive_count"]

    def inventory(self, run_id, player_id=None):
        run = self._get_run(run_id)
        player_id = player_id or run["account_id"]
        items = list(run["inventory"].get(player_id, []))
        items.extend(run["pending"].get(player_id, []))
        return items

    def leaderboard(self):
        return [dict(entry) for entry in self._leaderboard]

    def snapshot(self):
        return copy.deepcopy({
            "server_day": self.server_day,
            "seed_base": self.seed_base,
            "content_version": self.content_version,
            "runs": self._runs,
            "player_runs": self._player_runs,
            "settlements": self._settlements,
            "leaderboard": self._leaderboard,
        })

    def restore(self, snapshot):
        data = copy.deepcopy(snapshot)
        self.server_day = data["server_day"]
        self.seed_base = data["seed_base"]
        # content_version intentionally stays at the client's current
        # version so recover() can reject snapshots from older builds.
        self._runs = data["runs"]
        self._player_runs = data["player_runs"]
        self._settlements = data["settlements"]
        self._leaderboard = data["leaderboard"]
