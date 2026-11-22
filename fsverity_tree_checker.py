"""fs-verity Merkle tree and descriptor enforcement skeleton."""

UNSUPPORTED_ALGORITHM = "unsupported-algorithm"
UNTRUSTED_CERTIFICATE = "untrusted-certificate"
SIGNATURE_MISMATCH = "signature-mismatch"
READ_ONLY = "read-only"
STALE_VERSION = "stale-version"
IO_ERROR = "io-error"
INVALID_ARGUMENT = "invalid-argument"
OPERATION_FAILED = "operation-failed"


class FsverityError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class FsverityTreeChecker:
    """Builds Merkle trees bound to signatures and descriptors.

    Leaf blocks are the file split into block_size chunks with the final
    chunk zero-padded.  The root and signature are bound together; enabling
    verity is atomic and makes the inode read-only.  Descriptor reads carry
    a version so a replaced tree cannot be used with newer data.
    """

    def __init__(self, allowed_algorithms=("sha256",), block_size=4096,
                 trust_anchors=("CN=Root",)):
        self.allowed_algorithms = tuple(allowed_algorithms)
        self.block_size = block_size
        self.trust_anchors = tuple(trust_anchors)

    def build_tree(self, data, algorithm="sha256"):
        raise NotImplementedError

    def sign(self, tree, signer, chain):
        raise NotImplementedError

    def enable(self, inode, data, signature, chain, fail_at=None):
        raise NotImplementedError

    def verify(self, inode, data):
        raise NotImplementedError

    def read(self, inode, offset=0, length=None, version=None):
        raise NotImplementedError

    def write(self, inode, data):
        raise NotImplementedError

    def rename(self, inode, new_name):
        raise NotImplementedError

    def is_enabled(self, inode):
        raise NotImplementedError

    def current_version(self, inode):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
