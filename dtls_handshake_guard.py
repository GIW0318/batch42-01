"""DTLS handshake guard.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* ``client_hello`` returns a dict with ``action`` in
  ``{"hello_verify_request", "server_hello", "reject"}``.  A ClientHello
  without a valid cookie produces ``hello_verify_request`` and a cookie
  bound to (address, port) plus the client random.  Presenting a cookie on a
  different transport tuple is rejected with ``cookie_transport_mismatch``.
  A second ClientHello carrying a cookie that already produced a session is
  rejected with ``duplicate_client_hello``; concurrent duplicates must also
  leave exactly one session.
* ``finish_handshake`` moves a session to ``established``.
* ``application_data`` before ``established`` is dropped with alert
  ``unexpected_message``.  Once more than ``max_early_data`` early records
  were rejected the session is closed (``closed`` is True and
  ``connection_state`` is ``closed``).
* ``receive_record`` keeps an epoch-scoped receive window.  A later-arriving
  older sequence is accepted as ``out_of_order`` but never lowers the high
  water mark; a duplicate is rejected with ``replay`` True.  ``key_update``
  advances ``current_epoch`` and closes the old epoch: old-epoch records are
  rejected with ``stale_epoch``.
* ``protect`` returns a fresh nonce for every record under the same epoch.
* ``retransmission_delay`` is RTT based: ``max(1.0, 2 * rtt) * 2 ** attempt``
  capped at 60 seconds, so the delay never falls back to a fixed timer.
* ``fragment`` enforces ``max_fragment_bytes`` for the declared total length
  and for the cumulative buffered bytes, raising ``FragmentLimitExceeded``.
* ``verify_chain`` walks leaf -> intermediates -> self-signed root, requiring
  every issuer to be present, every certificate inside its validity window
  and every non-leaf to carry ``ca`` True.  Failures use the reasons
  ``incomplete_chain`` and ``certificate_expired``.  ``resume_session`` only
  resumes when the ticket signature is ``valid`` and the chain passes.
* ``export_state`` / ``load_state`` persist the cookie secret, pending
  cookies and unfinished handshakes so a restart keeps working.
* ``close`` releases the socket immediately (state ``closed``) and is
  idempotent.
"""


class FragmentLimitExceeded(Exception):
    """Raised when a handshake fragment exceeds the reassembly budget."""


class DtlsHandshakeGuard:
    def __init__(self, cookie_secret, max_fragment_bytes=65536, max_early_data=2):
        raise NotImplementedError

    def client_hello(self, five_tuple, hello, now=0.0):
        raise NotImplementedError

    def session_count(self):
        raise NotImplementedError

    def session_id_for(self, five_tuple):
        raise NotImplementedError

    def finish_handshake(self, session_id):
        raise NotImplementedError

    def connection_state(self, session_id):
        raise NotImplementedError

    def application_data(self, session_id, seq, payload, now=0.0):
        raise NotImplementedError

    def early_data_rejections(self, session_id):
        raise NotImplementedError

    def receive_record(self, session_id, epoch, seq, payload=b"", now=0.0):
        raise NotImplementedError

    def receive_high_water(self, session_id):
        raise NotImplementedError

    def key_update(self, session_id):
        raise NotImplementedError

    def current_epoch(self, session_id):
        raise NotImplementedError

    def protect(self, session_id, plaintext):
        raise NotImplementedError

    def note_rtt(self, session_id, rtt):
        raise NotImplementedError

    def retransmission_delay(self, session_id, attempt):
        raise NotImplementedError

    def fragment(self, session_id, msg_seq, offset, total_len, data):
        raise NotImplementedError

    def verify_chain(self, chain, now):
        raise NotImplementedError

    def resume_session(self, ticket, chain, now):
        raise NotImplementedError

    def close(self, session_id):
        raise NotImplementedError

    def socket_count(self):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
