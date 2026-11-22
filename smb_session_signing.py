"""SMB signing gate.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* ``negotiate`` accepts only SMB2/SMB3 dialects.  An SMB1-only client is
  refused at handshake time with ``STATUS_NOT_SUPPORTED`` and reason
  ``SMB1 not supported``.  With ``require_signing`` True, ``signing`` False
  is refused with ``signing required``; an algorithm list without
  ``AES-CMAC`` or ``HMAC-SHA256`` is refused with ``no supported signing
  algorithm``.  ``AES-CMAC`` wins when available.
* ``bind_channel`` gives every channel its own key.  ``sign_request`` signs
  with the channel key, so a signature made with the primary channel key
  fails on another channel.
* ``submit`` verifies encrypted payloads as ciphertext before any decrypt.
  A bad signature returns ``STATUS_ACCESS_DENIED``, disconnects the session
  and returns the reserved credit.  At most one in-flight request per
  session: a second one returns ``STATUS_REQUEST_NOT_ACCEPTED`` with reason
  ``concurrent request``; ``hold`` keeps the session busy until ``release``.
* ``share_rules`` is matched case-insensitively, so a rule on ``Finance``
  also covers ``FINANCE``.
* ``renegotiate`` tokens are single use; a replayed token returns
  ``STATUS_INVALID_PARAMETER`` with reason ``replayed negotiate`` and does
  not disable signing.
* ``expire_sessions`` invalidates the session, its leases and its handles;
  ``read_handle`` then returns ``STATUS_FILE_CLOSED``.
* Durable handles survive ``export_state`` / ``load_state`` but
  ``restore_handle`` must present the session key again or returns
  ``STATUS_ACCESS_DENIED``.
* ``query_directory`` reports ``STATUS_BUFFER_OVERFLOW`` with
  ``truncated`` True when the encoded listing exceeds 64 KiB; it never
  claims ``STATUS_SUCCESS`` for a truncated listing.
* ``rename`` keeps the open handle valid and updates its path.
* Audit entries never contain the raw NTLM response.
"""


class SmbSigningGate:
    def __init__(self, require_signing=True, share_rules=None):
        raise NotImplementedError

    def negotiate(
        self,
        client_id,
        dialects,
        algorithms,
        signing=True,
        session_id=None,
        now=0.0,
        ttl=300.0,
    ):
        raise NotImplementedError

    def session_key(self, session_id):
        raise NotImplementedError

    def bind_channel(self, session_id, channel_id, channel_key):
        raise NotImplementedError

    def sign_request(self, session_id, channel_id, payload):
        raise NotImplementedError

    def submit(
        self,
        session_id,
        channel_id,
        share,
        path,
        payload,
        signature,
        request_id=None,
        ciphertext=None,
        encrypted=False,
        ntlm_response=None,
        hold=False,
    ):
        raise NotImplementedError

    def release(self, session_id):
        raise NotImplementedError

    def available_credits(self, session_id):
        raise NotImplementedError

    def grant_credits(self, session_id, count):
        raise NotImplementedError

    def is_connected(self, session_id):
        raise NotImplementedError

    def renegotiate(
        self, session_id, token, dialects, algorithms, signing=True
    ):
        raise NotImplementedError

    def open_handle(
        self,
        session_id,
        share,
        path,
        lease_ttl=60.0,
        now=0.0,
        durable=False,
        entries=None,
    ):
        raise NotImplementedError

    def read_handle(self, handle_id, now=0.0):
        raise NotImplementedError

    def query_directory(self, handle_id):
        raise NotImplementedError

    def rename(self, handle_id, new_path):
        raise NotImplementedError

    def handle_path(self, handle_id):
        raise NotImplementedError

    def lease_active(self, handle_id):
        raise NotImplementedError

    def expire_sessions(self, now):
        raise NotImplementedError

    def restore_handle(self, handle_id, session_key):
        raise NotImplementedError

    def audit_entries(self):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
