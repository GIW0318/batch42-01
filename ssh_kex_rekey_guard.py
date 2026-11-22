"""SSH transport rekey guard.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* ``open_connection`` picks the strongest common algorithm from
  ``("curve25519-sha256", "ecdh-sha2-nistp256",
  "diffie-hellman-group14-sha256")``.  If there is no common strong
  algorithm the connection is ``disconnected`` with reason exactly
  ``no matching key exchange method``.
* ``begin_rekey`` is single-flight per connection and bounded by
  ``max_pending``.  A second concurrent request returns ``accepted`` False
  with reason ``rekey in progress``; when the global pending budget is full
  the reason is ``rekey queue full``.
* ``send_data`` while a rekey is pending is refused (``sent`` False,
  ``buffered`` True, ``violation`` True) until ``complete_rekey`` swaps in
  the new key.  Sequence numbers continue across a rekey, they are never
  reset to zero, and key ids move ``k1`` -> ``k2`` only after NEWKEYS.
* ``consume_window`` refuses to move bytes while a rekey is pending
  (``window frozen``); after ``complete_rekey`` each channel window is
  recomputed from the negotiated initial window.
* ``advance_bytes`` uses a 64-bit counter, so wrapping past 2**32 still
  crosses the configured rekey threshold.
* ``check_timeout`` aborts a stalled rekey and closes the connection,
  releasing its channels and handles; nothing stays half-open.
* ``rotate_host_key`` replaces the trusted host key, so the previous key
  fails ``check_host_key`` with reason ``host key changed``.
* Debug entries never contain raw KEX payloads or session ids; they may
  contain lengths, hashes or redaction markers.
"""


class SshRekeyGuard:
    def __init__(self, rekey_bytes=1024 * 1024, rekey_timeout=30.0, max_pending=8):
        raise NotImplementedError

    def open_connection(
        self, conn_id, client_algorithms, server_algorithms, session_id=None
    ):
        raise NotImplementedError

    def is_open(self, conn_id):
        raise NotImplementedError

    def begin_rekey(self, conn_id, initiator="client", kexinit=None, now=0.0):
        raise NotImplementedError

    def complete_rekey(self, conn_id, rekey_id):
        raise NotImplementedError

    def abort_rekey(self, conn_id, rekey_id):
        raise NotImplementedError

    def send_data(self, conn_id, nbytes=1):
        raise NotImplementedError

    def violations(self, conn_id):
        raise NotImplementedError

    def active_key_id(self, conn_id):
        raise NotImplementedError

    def next_sequence(self, conn_id):
        raise NotImplementedError

    def advance_bytes(self, conn_id, nbytes):
        raise NotImplementedError

    def counter_bytes(self, conn_id):
        raise NotImplementedError

    def needs_rekey(self, conn_id):
        raise NotImplementedError

    def open_channel(self, conn_id, channel_id, window=1024):
        raise NotImplementedError

    def channel_window(self, channel_id):
        raise NotImplementedError

    def consume_window(self, channel_id, nbytes):
        raise NotImplementedError

    def close_channel(self, conn_id, channel_id):
        raise NotImplementedError

    def channel_count(self, conn_id):
        raise NotImplementedError

    def handle_count(self):
        raise NotImplementedError

    def check_timeout(self, now):
        raise NotImplementedError

    def pending_rekeys(self):
        raise NotImplementedError

    def rotate_host_key(self, host, new_key):
        raise NotImplementedError

    def trust_host_key(self, host, key):
        raise NotImplementedError

    def check_host_key(self, host, presented_key):
        raise NotImplementedError

    def set_debug(self, enabled=True):
        raise NotImplementedError

    def debug_entries(self):
        raise NotImplementedError
