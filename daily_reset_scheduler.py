class ResetConfigError(Exception):
    pass


class DailyReset:
    def __init__(self):
        self._accounts = {}
        self._executed = set()
        self._tasks = {}
        self._compensations = {}
        self._characters = {}
        self._maintenance_until = 0
        self._leaderboard = {}
        self._archive = {}
        self._progress = {}

    def set_account(self, account_id, offset_minutes, period_days=1,
                    char_ids=None):
        raise NotImplementedError()

    def next_reset_ms(self, account_id, from_ms):
        raise NotImplementedError()

    def batch_key(self, account_id, now_ms):
        raise NotImplementedError()

    def run(self, now_ms, fail_after=None, shard="default"):
        raise NotImplementedError()

    def run_account(self, account_id, now_ms):
        raise NotImplementedError()

    def submit_task(self, account_id, task_id, completed_ms, processed_ms):
        raise NotImplementedError()

    def task_period(self, account_id, task_id):
        raise NotImplementedError()

    def set_maintenance_until(self, ms):
        raise NotImplementedError()

    def set_character_account(self, char_id, account_id):
        raise NotImplementedError()

    def reset_character(self, char_id):
        raise NotImplementedError()

    def compensate(self, account_id, batch_key, reward_id):
        raise NotImplementedError()

    def compensations(self, account_id):
        raise NotImplementedError()

    def set_leaderboard(self, period, scores):
        raise NotImplementedError()

    def set_progress(self, account_id, progress, accepted=None):
        raise NotImplementedError()

    def leaderboard(self, period):
        raise NotImplementedError()

    def archive(self, period):
        raise NotImplementedError()

    def state(self, account_id):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()

    def restore(self, snapshot):
        raise NotImplementedError()
