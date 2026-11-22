"""fs-verity Merkle tree and descriptor enforcement."""

import base64
import copy
import hashlib
import hmac
import json
import struct
import threading


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

    _WEAK_ALGORITHMS = frozenset(("md4", "md5", "md5-sha1", "sha1"))
    _SIGNATURE_SCHEMA = 1
    _SNAPSHOT_SCHEMA = 1

    def __init__(self, allowed_algorithms=("sha256",), block_size=4096,
                 trust_anchors=("CN=Root",)):
        if isinstance(allowed_algorithms, str):
            allowed_algorithms = (allowed_algorithms,)
        if isinstance(trust_anchors, str):
            trust_anchors = (trust_anchors,)

        if not isinstance(block_size, int) or isinstance(block_size, bool):
            raise FsverityError(INVALID_ARGUMENT, "block_size must be an integer")
        if block_size <= 0:
            raise FsverityError(INVALID_ARGUMENT, "block_size must be positive")

        try:
            algorithms = tuple(str(value).strip().lower()
                               for value in allowed_algorithms)
            anchors = tuple(str(value) for value in trust_anchors)
        except TypeError:
            raise FsverityError(INVALID_ARGUMENT, "invalid checker configuration")

        self.allowed_algorithms = algorithms
        self.block_size = block_size
        self.trust_anchors = anchors
        self._lock = threading.RLock()
        self._inodes = {}

    def build_tree(self, data, algorithm="sha256"):
        data = self._require_bytes(data)
        return self._build_tree(data, algorithm, self.block_size)

    def sign(self, tree, signer, chain):
        if not isinstance(tree, dict):
            raise FsverityError(INVALID_ARGUMENT, "tree must be a mapping")
        if not isinstance(signer, str) or not signer:
            raise FsverityError(INVALID_ARGUMENT, "signer must be a name")

        algorithm = self._validate_algorithm(tree.get("algorithm"))
        self._validate_chain(chain)
        descriptor = self._descriptor_from_tree(tree)

        fields = self._signature_fields(descriptor, signer, chain)
        return {
            "schema": self._SIGNATURE_SCHEMA,
            "algorithm": algorithm,
            "root": descriptor["root"],
            "block_size": descriptor["block_size"],
            "data_size": descriptor["data_size"],
            "leaf_count": descriptor["leaf_count"],
            "levels": descriptor["levels"],
            "descriptor_digest": self._descriptor_digest(descriptor),
            "signer": signer,
            "chain": copy.deepcopy(list(chain)),
            "signature": self._signature_value(algorithm, fields),
        }

    def enable(self, inode, data, signature, chain, fail_at=None):
        self._validate_inode_key(inode)
        data = self._require_bytes(data)
        if not isinstance(signature, dict):
            raise FsverityError(INVALID_ARGUMENT, "signature must be a mapping")

        algorithm = self._validate_algorithm(signature.get("algorithm"))
        self._validate_chain(chain)

        with self._lock:
            existing = self._inodes.get(inode)
            if existing is not None and existing["enabled"]:
                raise FsverityError(OPERATION_FAILED, "inode already has verity")

            if fail_at in ("build", "tree"):
                raise FsverityError(OPERATION_FAILED, "tree build interrupted")

            tree = self._build_tree(data, algorithm, self.block_size)

            if fail_at in ("sign", "signature"):
                raise FsverityError(OPERATION_FAILED, "signing interrupted")

            descriptor = self._descriptor_from_tree(tree)
            self._validate_signature(tree, signature, chain, descriptor)

            if fail_at is not None:
                raise FsverityError(OPERATION_FAILED, "enable interrupted")

            version = 1 if existing is None else existing["version"] + 1
            name = existing["name"] if existing is not None else (
                inode if isinstance(inode, str) else None
            )
            state = {
                "name": name,
                "enabled": True,
                "data": data,
                "tree": copy.deepcopy(tree),
                "descriptor": copy.deepcopy(descriptor),
                "signature": copy.deepcopy(signature),
                "version": version,
            }
            self._inodes[inode] = state
            return True

    def verify(self, inode, data):
        self._validate_inode_key(inode)
        data = self._require_bytes(data)

        with self._lock:
            state = self._inodes.get(inode)
            if state is None:
                raise FsverityError(IO_ERROR, "inode not found")
            if not state["enabled"]:
                return False
            return self._verify_state_data(state, data)

    def read(self, inode, offset=0, length=None, version=None):
        self._validate_inode_key(inode)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise FsverityError(INVALID_ARGUMENT, "offset must be non-negative")
        if length is not None:
            if (not isinstance(length, int) or isinstance(length, bool)
                    or length < 0):
                raise FsverityError(
                    INVALID_ARGUMENT, "length must be non-negative"
                )
        if version is not None:
            if (not isinstance(version, int) or isinstance(version, bool)
                    or version < 0):
                raise FsverityError(
                    INVALID_ARGUMENT, "version must be non-negative"
                )

        with self._lock:
            state = self._inodes.get(inode)
            if state is None:
                raise FsverityError(IO_ERROR, "inode not found")
            if version is not None and version != state["version"]:
                raise FsverityError(STALE_VERSION, "descriptor is stale")

            data = state["data"]
            if state["enabled"] and not self._verify_state_data(state, data):
                raise FsverityError(IO_ERROR, "descriptor and tree disagree")

            if length is None:
                return data[offset:]
            return data[offset:offset + length]

    def write(self, inode, data):
        self._validate_inode_key(inode)
        data = self._require_bytes(data)

        with self._lock:
            existing = self._inodes.get(inode)
            if existing is not None and existing["enabled"]:
                raise FsverityError(READ_ONLY, "inode has verity enabled")

            version = 1 if existing is None else existing["version"] + 1
            name = existing["name"] if existing is not None else (
                inode if isinstance(inode, str) else None
            )
            self._inodes[inode] = {
                "name": name,
                "enabled": False,
                "data": data,
                "tree": None,
                "descriptor": None,
                "signature": None,
                "version": version,
            }
            return len(data)

    def rename(self, inode, new_name):
        self._validate_inode_key(inode)
        if not isinstance(new_name, str) or not new_name:
            raise FsverityError(INVALID_ARGUMENT, "new name must be non-empty")

        with self._lock:
            state = self._inodes.get(inode)
            if state is None:
                raise FsverityError(IO_ERROR, "inode not found")
            renamed = dict(state)
            renamed["name"] = new_name
            self._inodes[inode] = renamed
            return True

    def is_enabled(self, inode):
        try:
            self._validate_inode_key(inode)
        except FsverityError:
            return False
        with self._lock:
            state = self._inodes.get(inode)
            return bool(state is not None and state["enabled"])

    def current_version(self, inode):
        self._validate_inode_key(inode)
        with self._lock:
            state = self._inodes.get(inode)
            if state is None:
                raise FsverityError(IO_ERROR, "inode not found")
            return state["version"]

    def snapshot(self):
        with self._lock:
            entries = []
            for inode, state in self._inodes.items():
                entries.append({
                    "inode": inode,
                    "name": state["name"],
                    "enabled": state["enabled"],
                    "version": state["version"],
                    "data": base64.b64encode(state["data"]).decode("ascii"),
                    "descriptor": copy.deepcopy(state["descriptor"]),
                    "signature": copy.deepcopy(state["signature"]),
                })
            return {
                "schema": self._SNAPSHOT_SCHEMA,
                "checker": {
                    "allowed_algorithms": list(self.allowed_algorithms),
                    "block_size": self.block_size,
                    "trust_anchors": list(self.trust_anchors),
                },
                "inodes": entries,
            }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise FsverityError(INVALID_ARGUMENT, "snapshot must be a mapping")
        if snapshot.get("schema") != self._SNAPSHOT_SCHEMA:
            raise FsverityError(INVALID_ARGUMENT, "unsupported snapshot schema")
        entries = snapshot.get("inodes")
        if not isinstance(entries, list):
            raise FsverityError(INVALID_ARGUMENT, "snapshot inode list missing")

        restored = {}
        for entry in entries:
            inode, state = self._restore_entry(entry)
            if inode in restored:
                raise FsverityError(INVALID_ARGUMENT, "duplicate inode")
            restored[inode] = state

        with self._lock:
            self._inodes = restored

    def _restore_entry(self, entry):
        if not isinstance(entry, dict):
            raise FsverityError(INVALID_ARGUMENT, "invalid inode entry")

        inode = entry.get("inode")
        self._validate_inode_key(inode)
        data = self._decode_snapshot_data(entry.get("data"))
        enabled = entry.get("enabled")
        version = entry.get("version")
        name = entry.get("name")

        if not isinstance(enabled, bool):
            raise FsverityError(INVALID_ARGUMENT, "invalid enabled flag")
        if (not isinstance(version, int) or isinstance(version, bool)
                or version < 1):
            raise FsverityError(INVALID_ARGUMENT, "invalid inode version")
        if name is not None and not isinstance(name, str):
            raise FsverityError(INVALID_ARGUMENT, "invalid inode name")

        if enabled:
            descriptor = entry.get("descriptor")
            signature = entry.get("signature")
            if not isinstance(descriptor, dict) or not isinstance(signature, dict):
                raise FsverityError(
                    INVALID_ARGUMENT, "descriptor or signature missing"
                )

            algorithm = self._validate_algorithm(descriptor.get("algorithm"))
            tree = self._build_tree(data, algorithm, self.block_size)
            expected = self._descriptor_from_tree(tree)
            if not self._descriptors_equal(descriptor, expected):
                raise FsverityError(
                    SIGNATURE_MISMATCH, "stored descriptor does not match data"
                )

            chain = signature.get("chain")
            self._validate_signature(tree, signature, chain, expected)
            state = {
                "name": name,
                "enabled": True,
                "data": data,
                "tree": tree,
                "descriptor": expected,
                "signature": copy.deepcopy(signature),
                "version": version,
            }
        else:
            state = {
                "name": name,
                "enabled": False,
                "data": data,
                "tree": None,
                "descriptor": None,
                "signature": None,
                "version": version,
            }
        return inode, state

    def _build_tree(self, data, algorithm, block_size):
        algorithm = self._validate_algorithm(algorithm)
        data = self._require_bytes(data)
        if (not isinstance(block_size, int) or isinstance(block_size, bool)
                or block_size <= 0):
            raise FsverityError(INVALID_ARGUMENT, "invalid block size")

        digest_size = hashlib.new(algorithm).digest_size
        if data:
            blocks = [
                data[start:start + block_size]
                for start in range(0, len(data), block_size)
            ]
        else:
            blocks = [b""]

        leaf_hashes = []
        for index, block in enumerate(blocks):
            padded = block.ljust(block_size, b"\0")
            prefix = b"fsverity-leaf-v1\0" + struct.pack(
                ">QQ", len(data), index
            )
            leaf_hashes.append(
                hashlib.new(algorithm, prefix + padded).digest()
            )

        current = leaf_hashes
        levels = 1
        while len(current) > 1:
            capacity = max(1, block_size // digest_size)
            parents = []
            for start in range(0, len(current), capacity):
                group = current[start:start + capacity]
                prefix = b"fsverity-node-v1\0" + struct.pack(
                    ">QQI", len(data), levels, start
                )
                payload = prefix + b"".join(group)
                padded = payload.ljust(block_size, b"\0")
                parents.append(
                    hashlib.new(algorithm, padded).digest()
                )
            current = parents
            levels += 1

        root = current[0]
        return {
            "algorithm": algorithm,
            "block_size": block_size,
            "data_size": len(data),
            "leaf_count": len(leaf_hashes),
            "levels": levels,
            "digest_size": digest_size,
            "root": root.hex(),
            "root_bytes": root,
            "leaf_hashes": [digest.hex() for digest in leaf_hashes],
        }

    def _descriptor_from_tree(self, tree):
        if not isinstance(tree, dict):
            raise FsverityError(INVALID_ARGUMENT, "tree must be a mapping")

        algorithm = self._validate_algorithm(tree.get("algorithm"))
        block_size = tree.get("block_size")
        data_size = tree.get("data_size")
        leaf_count = tree.get("leaf_count")
        levels = tree.get("levels")
        root = tree.get("root")

        if (not isinstance(block_size, int) or isinstance(block_size, bool)
                or block_size <= 0):
            raise FsverityError(INVALID_ARGUMENT, "invalid tree block size")
        if (not isinstance(data_size, int) or isinstance(data_size, bool)
                or data_size < 0):
            raise FsverityError(INVALID_ARGUMENT, "invalid tree data size")
        if (not isinstance(leaf_count, int) or isinstance(leaf_count, bool)
                or leaf_count < 1):
            raise FsverityError(INVALID_ARGUMENT, "invalid tree leaf count")
        if (not isinstance(levels, int) or isinstance(levels, bool)
                or levels < 1):
            raise FsverityError(INVALID_ARGUMENT, "invalid tree levels")
        if not isinstance(root, str) or not root:
            raise FsverityError(INVALID_ARGUMENT, "invalid tree root")

        return {
            "algorithm": algorithm,
            "block_size": block_size,
            "data_size": data_size,
            "leaf_count": leaf_count,
            "levels": levels,
            "root": root,
        }

    def _validate_signature(self, tree, signature, chain, descriptor):
        algorithm = self._validate_algorithm(signature.get("algorithm"))
        self._validate_chain(chain)

        if algorithm != descriptor["algorithm"]:
            raise FsverityError(
                SIGNATURE_MISMATCH, "signature algorithm differs from tree"
            )
        if not self._secure_equal(
                signature.get("root"), descriptor["root"]):
            raise FsverityError(SIGNATURE_MISMATCH, "root is not signed")
        if signature.get("block_size") != descriptor["block_size"]:
            raise FsverityError(
                SIGNATURE_MISMATCH, "block size is not signed"
            )
        if signature.get("data_size") != descriptor["data_size"]:
            raise FsverityError(
                SIGNATURE_MISMATCH, "data size is not signed"
            )
        if signature.get("leaf_count") != descriptor["leaf_count"]:
            raise FsverityError(
                SIGNATURE_MISMATCH, "leaf count is not signed"
            )
        if signature.get("levels") != descriptor["levels"]:
            raise FsverityError(SIGNATURE_MISMATCH, "levels are not signed")

        expected_descriptor_digest = self._descriptor_digest(descriptor)
        if not self._secure_equal(
                signature.get("descriptor_digest"),
                expected_descriptor_digest):
            raise FsverityError(
                SIGNATURE_MISMATCH, "descriptor digest is not signed"
            )

        signer = signature.get("signer")
        if not isinstance(signer, str) or not signer:
            raise FsverityError(
                SIGNATURE_MISMATCH, "signer identity is missing"
            )
        signed_chain = signature.get("chain")
        if not self._same_chain(signed_chain, chain):
            raise FsverityError(
                SIGNATURE_MISMATCH, "certificate chain is not signed"
            )

        fields = self._signature_fields(descriptor, signer, chain)
        expected_signature = self._signature_value(algorithm, fields)
        if not self._secure_equal(
                signature.get("signature"), expected_signature):
            raise FsverityError(
                SIGNATURE_MISMATCH, "signature value is invalid"
            )

    def _verify_state_data(self, state, data):
        if not state.get("enabled"):
            return False
        descriptor = state.get("descriptor")
        if not isinstance(descriptor, dict):
            return False
        try:
            tree = self._build_tree(
                data,
                descriptor.get("algorithm"),
                descriptor.get("block_size"),
            )
            expected = self._descriptor_from_tree(tree)
        except FsverityError:
            return False
        return self._descriptors_equal(descriptor, expected)

    def _descriptors_equal(self, left, right):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        for key in ("algorithm", "block_size", "data_size", "leaf_count",
                    "levels"):
            if left.get(key) != right.get(key):
                return False
        return self._secure_equal(left.get("root"), right.get("root"))

    def _descriptor_digest(self, descriptor):
        algorithm = self._validate_algorithm(descriptor.get("algorithm"))
        payload = b"fsverity-descriptor-v1\0" + self._canonical(descriptor)
        return hashlib.new(algorithm, payload).hexdigest()

    def _signature_fields(self, descriptor, signer, chain):
        return {
            "descriptor": descriptor,
            "signer": signer,
            "chain": list(chain),
        }

    def _signature_value(self, algorithm, fields):
        payload = b"fsverity-signature-v1\0" + self._canonical(fields)
        return hashlib.new(algorithm, payload).hexdigest()

    def _validate_chain(self, chain):
        if not isinstance(chain, (list, tuple)) or not chain:
            raise FsverityError(
                UNTRUSTED_CERTIFICATE, "certificate chain is empty"
            )

        certificates = []
        for certificate in chain:
            if not isinstance(certificate, dict):
                raise FsverityError(
                    UNTRUSTED_CERTIFICATE, "invalid certificate"
                )
            subject = certificate.get("subject")
            issuer = certificate.get("issuer")
            if not isinstance(subject, str) or not subject:
                raise FsverityError(
                    UNTRUSTED_CERTIFICATE, "certificate subject missing"
                )
            if not isinstance(issuer, str) or not issuer:
                raise FsverityError(
                    UNTRUSTED_CERTIFICATE, "certificate issuer missing"
                )
            valid = certificate.get("signature_valid")
            if valid is None:
                valid = certificate.get("valid")
            if valid is not True:
                raise FsverityError(
                    UNTRUSTED_CERTIFICATE, "certificate signature invalid"
                )
            certificates.append(certificate)

        by_subject = {}
        for certificate in certificates:
            by_subject.setdefault(certificate["subject"], certificate)

        anchors = [
            certificate for certificate in certificates
            if (certificate["subject"] in self.trust_anchors
                and certificate["issuer"] == certificate["subject"]
                and certificate.get("trusted") is True)
        ]
        if not anchors:
            raise FsverityError(
                UNTRUSTED_CERTIFICATE, "no trusted certificate anchor"
            )

        anchor_ids = {id(certificate) for certificate in anchors}
        for certificate in certificates:
            current = certificate
            seen = set()
            while id(current) not in anchor_ids:
                current_id = id(current)
                if current_id in seen:
                    raise FsverityError(
                        UNTRUSTED_CERTIFICATE, "certificate chain has a cycle"
                    )
                seen.add(current_id)
                current = by_subject.get(current["issuer"])
                if current is None:
                    raise FsverityError(
                        UNTRUSTED_CERTIFICATE,
                        "certificate does not chain to an anchor",
                    )

    def _validate_algorithm(self, algorithm):
        if not isinstance(algorithm, str) or not algorithm.strip():
            raise FsverityError(
                UNSUPPORTED_ALGORITHM, "algorithm must be a name"
            )
        algorithm = algorithm.strip().lower()
        if (algorithm in self._WEAK_ALGORITHMS
                or algorithm not in self.allowed_algorithms
                or algorithm not in hashlib.algorithms_available):
            raise FsverityError(
                UNSUPPORTED_ALGORITHM, "algorithm is not allowed"
            )
        return algorithm

    def _same_chain(self, left, right):
        if not isinstance(left, (list, tuple)) or not isinstance(
                right, (list, tuple)):
            return False
        if len(left) != len(right):
            return False
        return self._canonical(left) == self._canonical(right)

    def _canonical(self, value):
        try:
            return json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        except (TypeError, ValueError):
            raise FsverityError(INVALID_ARGUMENT, "value is not serializable")

    def _secure_equal(self, left, right):
        if isinstance(left, str) and isinstance(right, str):
            left = left.encode("utf-8")
            right = right.encode("utf-8")
        if not isinstance(left, bytes) or not isinstance(right, bytes):
            return False
        return hmac.compare_digest(left, right)

    def _require_bytes(self, data):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise FsverityError(INVALID_ARGUMENT, "data must be bytes")
        return bytes(data)

    def _decode_snapshot_data(self, value):
        if isinstance(value, bytes):
            return value
        if not isinstance(value, str):
            raise FsverityError(
                INVALID_ARGUMENT, "snapshot data is invalid"
            )
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError):
            raise FsverityError(
                INVALID_ARGUMENT, "snapshot data is not valid base64"
            )

    def _validate_inode_key(self, inode):
        if inode is None:
            raise FsverityError(INVALID_ARGUMENT, "inode must not be None")
        try:
            hash(inode)
        except TypeError:
            raise FsverityError(INVALID_ARGUMENT, "inode must be hashable")
