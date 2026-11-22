"""CoAP Block1 reassembly.

Baseline shell: every method raises NotImplementedError.

Contract notes for the implementation:

* Uploads are keyed by ``(token, resource)``.  ``put_block`` answers with a
  CoAP code string and the relevant pacing fields.
* Blocks must arrive in ascending, contiguous order.  A gap (including a
  first block numbered above zero or a final block ahead of the window)
  returns ``4.08 Request Entity Incomplete`` with ``expected_block`` set;
  it must never answer ``2.31 Continue`` for a missing block.
* A retransmitted block below the expected number is accepted idempotently
  with ``duplicate`` True and is not appended a second time.
* The first block fixes ``block_size`` and the ETag.  A later block with a
  different size rejects with ``4.08`` and discards the assembly.  A
  differing ETag on a later block also rejects with ``4.08`` and discards
  the whole assembly.  A second block zero with a different ETag while the
  current upload is incomplete is a token collision: ``4.09 Conflict`` and
  the first upload stays intact.
* ``block_num`` outside ``0..65535`` raises ``BlockNumberOutOfRange``.
* A block size above ``max_block_size`` returns ``4.13 Request Entity Too
  Large`` with ``szx`` set to the supported size.  Exceeding
  ``max_total_bytes`` also returns ``4.13`` and discards the buffer.
* ``expire`` drops uploads idle longer than ``timeout`` and frees their
  buffered bytes.
* ``export_state`` / ``load_state`` preserve partial uploads so a restart
  resumes at the same block.
"""


class BlockNumberOutOfRange(Exception):
    """Raised when a Block1 number does not fit the 16 bit option."""


class CoapBlockAssembler:
    def __init__(
        self, max_total_bytes=8 * 1024 * 1024, max_block_size=1024, timeout=30.0
    ):
        raise NotImplementedError

    def put_block(
        self,
        token,
        resource,
        block_num,
        more,
        payload,
        block_size=1024,
        etag=None,
        now=0.0,
    ):
        raise NotImplementedError

    def pending_uploads(self):
        raise NotImplementedError

    def buffered_bytes(self):
        raise NotImplementedError

    def expire(self, now):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def load_state(self, state):
        raise NotImplementedError
