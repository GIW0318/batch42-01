import copy
import math
import threading
from datetime import date, timedelta
from typing import NamedTuple


DAY_MS = 86_400_000
_EPOCH_DATE = date(1970, 1, 1)


class ResetConfigError(Exception):
    pass


class Compensation(NamedTuple):
    batch_key: str
    reward_id: str


class DailyReset:
    def __init__(self):
        self._accounts = {}
        self._executed = set()
        self._batch_table = {}
        self._tasks = {}
        self._compensations = {}
        self._compensation_keys = set()
        self._characters = {}
        self._character_resets = set()
        self._maintenance_until = 0
        self._leaderboard = {}
        self._archive = {}
        self._progress = {}
        self._reset_log = []
        self._lock = threading.RLock()

    def set_account(self, account_id, offset_minutes, period_days=1,
                    char_ids=None):
        offset = self._validate_offset(offset_minutes)
        period = self._validate_period(period_days)
        chars = self._normalise_char_ids(char_ids)

        with self._lock:
            account = self._accounts.get(account_id)
            if account is None:
                account = {
                    "offset_minutes": offset,
                    "period_days": period,
                    "char_ids": [],
                    "next_due_ms": self._next_reset_for(offset, period, 0),
                    "last_boundary_ms": None,
                    "last_reset_ms": None,
                }
                self._accounts[account_id] = account
            else:
                account["offset_minutes"] = offset
                account["period_days"] = period
                last_boundary = account.get("last_boundary_ms")
                if last_boundary is None:
                    account["next_due_ms"] = self._next_reset_for(
                        offset, period, 0
                    )
                else:
                    account["next_due_ms"] = self._next_reset_for(
                        offset, period, last_boundary
                    )

            for char_id in chars:
                if char_id not in account["char_ids"]:
                    account["char_ids"].append(char_id)
                self._characters.setdefault(char_id, account_id)

    def next_reset_ms(self, account_id, from_ms):
        with self._lock:
            account = self._get_account(account_id)
            return self._next_reset_for(
                account["offset_minutes"],
                account["period_days"],
                from_ms,
            )

    def batch_key(self, account_id, now_ms):
        with self._lock:
            account = self._get_account(account_id)
            return self._batch_key_for(account, now_ms)

    def run(self, now_ms, fail_after=None, shard="default"):
        now_ms = self._validate_timestamp(now_ms)
        if fail_after is not None and fail_after <= 0:
            return []

        with self._lock:
            if now_ms < self._maintenance_until:
                return []

            due_accounts = []
            for account_id in list(self._accounts):
                account = self._accounts[account_id]
                boundary = self._due_boundary(account, now_ms)
                if boundary is None:
                    continue

                batch = self._batch_key_for(account, boundary)
                key = (account_id, account["period_days"], batch)
                if key in self._executed:
                    self._skip_executed(account, now_ms, boundary)
                    continue
                self._batch_table[key] = {
                    "boundary_ms": boundary,
                    "offset_minutes": account["offset_minutes"],
                    "status": "pending",
                    "shard": shard,
                }
                due_accounts.append((account_id, account, boundary, batch))

            reset_accounts = []
            for account_id, account, boundary, batch in due_accounts:
                self._execute_account(
                    account_id, account, boundary, batch, shard
                )
                reset_accounts.append(account_id)

                if fail_after is not None and len(reset_accounts) >= fail_after:
                    break

            return reset_accounts

    def run_account(self, account_id, now_ms):
        now_ms = self._validate_timestamp(now_ms)
        with self._lock:
            account = self._get_account(account_id)
            if now_ms < self._maintenance_until:
                return False
            boundary = self._due_boundary(account, now_ms)
            if boundary is None:
                return False

            batch = self._batch_key_for(account, boundary)
            key = (account_id, account["period_days"], batch)
            if key in self._executed:
                self._skip_executed(account, now_ms, boundary)
                return False

            self._execute_account(
                account_id, account, boundary, batch, "manual"
            )
            return True

    def submit_task(self, account_id, task_id, completed_ms, processed_ms):
        completed_ms = self._validate_timestamp(completed_ms)
        processed_ms = self._validate_timestamp(processed_ms)
        with self._lock:
            account = self._get_account(account_id)
            period = self._batch_key_for(account, completed_ms)
            self._tasks[(account_id, task_id)] = {
                "completed_ms": completed_ms,
                "processed_ms": processed_ms,
                "period": period,
            }
            return period

    def task_period(self, account_id, task_id):
        with self._lock:
            return self._tasks[(account_id, task_id)]["period"]

    def set_maintenance_until(self, ms):
        self._maintenance_until = self._validate_timestamp(ms)

    def set_character_account(self, char_id, account_id):
        with self._lock:
            self._characters[char_id] = account_id
            account = self._accounts.get(account_id)
            if account is not None and char_id not in account["char_ids"]:
                account["char_ids"].append(char_id)

    def reset_character(self, char_id):
        with self._lock:
            return self._reset_character_once(char_id)

    def compensate(self, account_id, batch_key, reward_id):
        with self._lock:
            key = (account_id, batch_key, reward_id)
            if key in self._compensation_keys:
                return False
            self._compensation_keys.add(key)
            self._compensations.setdefault(account_id, []).append(
                Compensation(batch_key, reward_id)
            )
            return True

    def compensations(self, account_id):
        with self._lock:
            return list(self._compensations.get(account_id, []))

    def set_leaderboard(self, period, scores):
        with self._lock:
            self._leaderboard[period] = copy.deepcopy(dict(scores or {}))

    def set_progress(self, account_id, progress, accepted=None):
        with self._lock:
            self._get_account(account_id)
            self._progress[account_id] = {
                "progress": copy.deepcopy(progress),
                "accepted": copy.deepcopy(
                    self._normalise_values(accepted)
                ),
            }

    def leaderboard(self, period):
        with self._lock:
            return copy.deepcopy(self._leaderboard.get(period, {}))

    def archive(self, period):
        with self._lock:
            return copy.deepcopy(self._archive.get(period, {}))

    def state(self, account_id):
        with self._lock:
            self._get_account(account_id)
            progress = self._progress.get(
                account_id, {"progress": 0, "accepted": []}
            )
            return {
                "progress": copy.deepcopy(progress["progress"]),
                "accepted": copy.deepcopy(list(progress["accepted"])),
            }

    def snapshot(self):
        with self._lock:
            accounts = {
                account_id: {
                    "offset_minutes": account["offset_minutes"],
                    "period_days": account["period_days"],
                    "char_ids": list(account["char_ids"]),
                    "next_due_ms": account["next_due_ms"],
                    "last_boundary_ms": account["last_boundary_ms"],
                    "last_reset_ms": account.get("last_reset_ms"),
                }
                for account_id, account in self._accounts.items()
            }
            batch_table = [
                {
                    "account_id": key[0],
                    "period_days": key[1],
                    "batch_key": key[2],
                    **record,
                }
                for key, record in self._batch_table.items()
            ]
            tasks = [
                {
                    "account_id": key[0],
                    "task_id": key[1],
                    **record,
                }
                for key, record in self._tasks.items()
            ]
            compensations = [
                {
                    "account_id": account_id,
                    "batch_key": item.batch_key,
                    "reward_id": item.reward_id,
                }
                for account_id, items in self._compensations.items()
                for item in items
            ]

            return {
                "version": 1,
                "accounts": copy.deepcopy(accounts),
                "executed": [
                    [account_id, period_days, batch]
                    for account_id, period_days, batch in self._executed
                ],
                "batch_table": copy.deepcopy(batch_table),
                "tasks": copy.deepcopy(tasks),
                "compensations": copy.deepcopy(compensations),
                "characters": copy.deepcopy(self._characters),
                "character_resets": list(self._character_resets),
                "maintenance_until": self._maintenance_until,
                "leaderboard": copy.deepcopy(self._leaderboard),
                "archive": copy.deepcopy(self._archive),
                "progress": copy.deepcopy(self._progress),
                "reset_log": copy.deepcopy(self._reset_log),
            }

    def restore(self, snapshot):
        with self._lock:
            snapshot = copy.deepcopy(snapshot)
            self._accounts = {}
            for account_id, account in snapshot.get("accounts", {}).items():
                self._accounts[account_id] = {
                    "offset_minutes": account["offset_minutes"],
                    "period_days": account["period_days"],
                    "char_ids": list(account.get("char_ids", [])),
                    "next_due_ms": account.get(
                        "next_due_ms",
                        self._next_reset_for(
                            account["offset_minutes"],
                            account["period_days"],
                            0,
                        ),
                    ),
                    "last_boundary_ms": account.get("last_boundary_ms"),
                    "last_reset_ms": account.get("last_reset_ms"),
                }

            self._executed = {
                (account_id, period_days, batch)
                for account_id, period_days, batch
                in snapshot.get("executed", [])
            }
            self._batch_table = {}
            for record in snapshot.get("batch_table", []):
                record = dict(record)
                key = (
                    record.pop("account_id"),
                    record.pop("period_days"),
                    record.pop("batch_key"),
                )
                self._batch_table[key] = record

            self._tasks = {}
            for record in snapshot.get("tasks", []):
                record = dict(record)
                key = (record.pop("account_id"), record.pop("task_id"))
                self._tasks[key] = record

            self._compensations = {}
            self._compensation_keys = set()
            for record in snapshot.get("compensations", []):
                account_id = record["account_id"]
                item = Compensation(record["batch_key"], record["reward_id"])
                self._compensations.setdefault(account_id, []).append(item)
                self._compensation_keys.add(
                    (account_id, item.batch_key, item.reward_id)
                )

            self._characters = dict(snapshot.get("characters", {}))
            self._character_resets = set(
                snapshot.get("character_resets", [])
            )
            self._maintenance_until = snapshot.get("maintenance_until", 0)
            self._leaderboard = copy.deepcopy(
                snapshot.get("leaderboard", {})
            )
            self._archive = copy.deepcopy(snapshot.get("archive", {}))
            self._progress = copy.deepcopy(snapshot.get("progress", {}))
            self._reset_log = copy.deepcopy(snapshot.get("reset_log", []))

    def _execute_account(self, account_id, account, boundary, batch, shard):
        key = (account_id, account["period_days"], batch)
        if key in self._executed:
            return False

        self._batch_table[key] = {
            "boundary_ms": boundary,
            "offset_minutes": account["offset_minutes"],
            "status": "pending",
            "shard": shard,
        }

        self._archive_current_leaderboard(account["period_days"])
        self._clear_progress(account_id)
        for char_id in list(account["char_ids"]):
            self._reset_character_once(char_id)

        self._executed.add(key)
        self._batch_table[key]["status"] = "done"
        account["last_boundary_ms"] = boundary
        account["last_reset_ms"] = boundary
        account["next_due_ms"] = self._next_reset_for(
            account["offset_minutes"],
            account["period_days"],
            boundary,
        )
        self._reset_log.append({
            "account_id": account_id,
            "batch_key": batch,
            "boundary_ms": boundary,
            "offset_minutes": account["offset_minutes"],
            "period_days": account["period_days"],
            "shard": shard,
        })
        return True

    def _clear_progress(self, account_id):
        self._progress[account_id] = {"progress": 0, "accepted": []}

    def _reset_character_once(self, char_id):
        if char_id in self._character_resets:
            return False

        associated = set()
        mapped_account = self._characters.get(char_id)
        if mapped_account is not None:
            associated.add(mapped_account)
        for account_id, account in self._accounts.items():
            if char_id in account["char_ids"]:
                associated.add(account_id)

        if not associated:
            return False

        for account_id in associated:
            if account_id in self._accounts:
                self._clear_progress(account_id)
        self._character_resets.add(char_id)
        return True

    def _archive_current_leaderboard(self, period_days):
        period = self._leaderboard_period(period_days)
        if period in self._leaderboard:
            self._archive[period] = copy.deepcopy(self._leaderboard[period])
            del self._leaderboard[period]

    @staticmethod
    def _leaderboard_period(period_days):
        if period_days == 1:
            return "daily"
        if period_days == 7:
            return "weekly"
        return f"{period_days}d"

    def _skip_executed(self, account, now_ms, boundary):
        account["last_boundary_ms"] = boundary
        account["last_reset_ms"] = boundary
        account["next_due_ms"] = self._next_reset_for(
            account["offset_minutes"],
            account["period_days"],
            now_ms,
        )

    def _due_boundary(self, account, now_ms):
        boundary = self._current_period_boundary(account, now_ms)
        if boundary < account["next_due_ms"] or boundary > now_ms:
            return None
        last_reset = account.get("last_reset_ms")
        if last_reset is not None:
            minimum_gap = account["period_days"] * DAY_MS // 2
            if boundary - last_reset < minimum_gap:
                return None
        return boundary

    def _current_period_boundary(self, account, now_ms):
        period_ms = account["period_days"] * DAY_MS
        offset_ms = account["offset_minutes"] * 60_000
        local_ms = now_ms + offset_ms
        period_index = local_ms // period_ms
        return period_index * period_ms - offset_ms

    def _get_account(self, account_id):
        try:
            return self._accounts[account_id]
        except KeyError:
            raise KeyError(f"unknown account: {account_id!r}") from None

    def _batch_key_for(self, account, now_ms):
        now_ms = self._validate_timestamp(now_ms)
        period_ms = account["period_days"] * DAY_MS
        offset_ms = account["offset_minutes"] * 60_000
        local_ms = now_ms + offset_ms
        period_index = local_ms // period_ms
        local_period_start = period_index * period_ms
        day_index = local_period_start // DAY_MS
        return (_EPOCH_DATE + timedelta(days=day_index)).isoformat()

    def _next_reset_for(self, offset_minutes, period_days, from_ms):
        from_ms = self._validate_timestamp(from_ms)
        period_ms = period_days * DAY_MS
        offset_ms = offset_minutes * 60_000
        local_ms = from_ms + offset_ms
        period_index = local_ms // period_ms
        next_local = (period_index + 1) * period_ms
        return next_local - offset_ms

    @staticmethod
    def _validate_offset(offset_minutes):
        if isinstance(offset_minutes, bool) or not isinstance(
                offset_minutes, (int, float)):
            raise ResetConfigError("timezone offset must be a number")
        if not math.isfinite(offset_minutes):
            raise ResetConfigError("timezone offset must be finite")
        if offset_minutes != int(offset_minutes):
            raise ResetConfigError(
                "timezone offset must use whole minutes"
            )
        if not -1440 < offset_minutes < 1440:
            raise ResetConfigError(
                "timezone offset must be between -24h and +24h"
            )
        return int(offset_minutes)

    @staticmethod
    def _validate_period(period_days):
        if isinstance(period_days, bool) or not isinstance(
                period_days, (int, float)):
            raise ResetConfigError("reset period must be a number of days")
        if not math.isfinite(period_days):
            raise ResetConfigError("reset period must be finite")
        if period_days != int(period_days):
            raise ResetConfigError("reset period must be whole days")
        if period_days <= 0:
            raise ResetConfigError("reset period must be positive")
        return int(period_days)

    @staticmethod
    def _validate_timestamp(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ResetConfigError("timestamp must be a number")
        if not math.isfinite(value):
            raise ResetConfigError("timestamp must be finite")
        return int(value)

    @staticmethod
    def _normalise_char_ids(char_ids):
        return DailyReset._normalise_values(char_ids)

    @staticmethod
    def _normalise_values(values):
        if values is None:
            return []
        if isinstance(values, (str, bytes)):
            return [values]
        return list(dict.fromkeys(values))
