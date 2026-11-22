"""RADIUS accounting ledger.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* ``make_authenticator`` derives a stable digest from the NAS shared secret
  and the normalised packet fields.  ``process`` drops a packet whose
  authenticator does not match the secret of its NAS: status ``rejected``,
  reason ``bad authenticator`` and no ledger row.
* Session identity is ``(nas, session_id.strip().lower(),
  user.strip().lower())``.
* A repeated Start for an open session (or a closed record with the same
  ``session_time``) returns ``duplicate_start`` and creates no second row.
  A Start with the same id but a new ``session_time`` after the previous
  record closed starts a new session.
* Interims never regress usage: a lower octet count returns
  ``stale_interim`` and keeps the higher value.  An Interim after Stop
  returns ``late_interim`` and cannot reopen the session.
* Stop bills exactly once.  Repeated Stop returns ``duplicate_stop`` with
  ``billed`` False.  Stop before any Interim bills the Stop octet count.
* When ``min_interim_interval`` is positive, an Interim arriving too early
  is coalesced: status ``coalesced`` but usage still takes the maximum.
* Unknown accounting status is rejected as ``unknown_status`` with no row.
* ``export_state`` / ``load_state`` preserve open and closed sessions plus
  billed totals so restart keeps Stop idempotency.
"""


class RadiusAcctLedger:
    def __init__(self, secrets=None, min_interim_interval=0.0):
        raise NotImplementedError

    def make_authenticator(
        self, nas, session_id, status, octets, session_time=0
    ):
        raise NotImplementedError

    def process(self, packet, now=0.0):
        raise NotImplementedError

    def session_usage(self, nas, session_id):
        raise NotImplementedError

    def billed_total(self, user):
        raise NotImplementedError

    def open_sessions(self):
        raise NotImplementedError

    def session_count(self):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
