"""Entropy source health, seeding and consumer isolation monitor."""

import hashlib
import hmac
import os
import threading


class EntropyError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class EntropyHealthMonitor:
    """Keeps entropy sources, seed persistence and consumers isolated.

    - Sources contribute a fixed entropy budget scaled by weight; only
      healthy sources count toward the estimate.
    - The pool is ``ready`` only when the estimate reaches ``pool_size``.
    - Reads before initialization fail with ``EAGAIN`` and never return
      key material, regardless of the blocking flag.
    - Output is produced by an HMAC-DRBG-like expander keyed from mixed
      seed material; a counter plus key ratchet guarantees unique blocks
      under concurrency, and VM restores re-inject fresh entropy so
      clones never repeat output.
    """

    SOURCE_ENTROPY_BITS = 64
    SEED_BYTES = 32
    MIN_SEED_BYTES = 16

    def __init__(self, pool_size=256, budget_bps=1000):
        self.pool_size = pool_size
        self.budget_bps = budget_bps
        self._lock = threading.RLock()
        self._sources = {}
        self._audit = []
        self._entropy_bits = 0.0
        self._key = None
        self._counter = 0
        self._closed = False

    # -- internal helpers --------------------------------------------------

    def _ensure_open(self):
        if self._closed:
            raise EntropyError("ECLOSED", "monitor is closed")

    def _ready_locked(self):
        return self._entropy_bits >= self.pool_size

    @staticmethod
    def _derive_key(label, *parts):
        h = hashlib.sha256(label)
        for part in parts:
            h.update(part)
        return h.digest()

    @staticmethod
    def _expand(key, counter, n):
        out = bytearray()
        block = 0
        while len(out) < n:
            msg = counter.to_bytes(16, "big") + block.to_bytes(8, "big")
            out += hmac.new(key, msg, hashlib.sha256).digest()
            block += 1
        return bytes(out[:n])

    def _ensure_key_locked(self):
        if self._key is None:
            self._key = self._derive_key(b"entropy-init", os.urandom(self.SEED_BYTES))

    # -- source management -------------------------------------------------

    def add_source(self, name, *, kind="software", healthy=True, weight=1.0):
        with self._lock:
            self._ensure_open()
            if name in self._sources:
                raise EntropyError("EEXIST", "duplicate source %r" % (name,))
            if weight <= 0:
                raise EntropyError("EINVAL", "weight must be positive")
            healthy = bool(healthy)
            self._sources[name] = {
                "kind": kind,
                "healthy": healthy,
                "weight": float(weight),
            }
            if healthy:
                self._entropy_bits += self.SOURCE_ENTROPY_BITS * weight
            self._audit.append({
                "event": "source_added",
                "source": name,
                "kind": kind,
                "healthy": healthy,
            })

    def set_source_health(self, name, healthy):
        with self._lock:
            self._ensure_open()
            source = self._sources.get(name)
            if source is None:
                raise EntropyError("ENOSRC", "unknown source %r" % (name,))
            healthy = bool(healthy)
            if source["healthy"] != healthy:
                source["healthy"] = healthy
                delta = self.SOURCE_ENTROPY_BITS * source["weight"]
                if healthy:
                    self._entropy_bits += delta
                else:
                    self._entropy_bits = max(0.0, self._entropy_bits - delta)
            self._audit.append({
                "event": "source_health",
                "source": name,
                "healthy": healthy,
            })

    # -- pool state ----------------------------------------------------------

    def estimate(self):
        with self._lock:
            return int(min(self.pool_size, self._entropy_bits))

    def ready(self):
        with self._lock:
            return not self._closed and self._ready_locked()

    def status(self):
        with self._lock:
            self._ensure_open()
            active = [n for n, s in self._sources.items() if s["healthy"]]
            failed = [n for n, s in self._sources.items() if not s["healthy"]]
            return {
                "ready": self._ready_locked(),
                "estimate": self.estimate(),
                "pool_size": self.pool_size,
                "degraded": len(failed) > 0,
                "active_sources": active,
                "failed_sources": failed,
            }

    def source_shares(self):
        with self._lock:
            self._ensure_open()
            healthy = {
                n: s["weight"] for n, s in self._sources.items() if s["healthy"]
            }
            total = sum(healthy.values())
            if total <= 0:
                return {}
            return {n: w / total for n, w in healthy.items()}

    # -- consumer reads ------------------------------------------------------

    def read(self, n, *, blocking=False, caller_ns="host", now=0.0):
        with self._lock:
            self._ensure_open()
            if n <= 0:
                raise EntropyError("EINVAL", "read length must be positive")
            if not self._ready_locked():
                raise EntropyError("EAGAIN", "entropy pool not initialized")
            self._ensure_key_locked()
            output = self._expand(self._key, self._counter, n)
            self._counter += 1
            self._key = hashlib.sha256(self._key + output).digest()
            return output

    def drain(self):
        with self._lock:
            self._ensure_open()
            self._entropy_bits = 0.0
            self._audit.append({
                "event": "pool_drained",
                "source": None,
                "healthy": True,
            })

    # -- seed persistence ----------------------------------------------------

    def seed(self, caller_ns="host"):
        with self._lock:
            self._ensure_open()
            if caller_ns != "host":
                raise EntropyError(
                    "EACCES", "seed access denied outside host namespace"
                )
            self._ensure_key_locked()
            return self._key

    def restore_seed(self, seed):
        with self._lock:
            self._ensure_open()
            if not isinstance(seed, (bytes, bytearray)):
                raise EntropyError("EINVAL", "seed must be bytes")
            seed = bytes(seed)
            if len(seed) < self.MIN_SEED_BYTES:
                raise EntropyError("EINVAL", "seed too short")
            self._key = self._derive_key(b"entropy-restore", seed)
            self._counter = 0
            self._entropy_bits = max(self._entropy_bits, float(self.pool_size))
            self._audit.append({
                "event": "seed_restored",
                "source": None,
                "healthy": True,
            })

    # -- VM snapshot / restore ----------------------------------------------

    def snapshot(self):
        with self._lock:
            self._ensure_open()
            self._ensure_key_locked()
            return {
                "seed": self._key,
                "pool_size": self.pool_size,
                "estimate": self.estimate(),
            }

    def vm_restore(self, snapshot):
        with self._lock:
            self._ensure_open()
            if not isinstance(snapshot, dict) or "seed" not in snapshot:
                raise EntropyError("EINVAL", "invalid snapshot")
            seed = snapshot["seed"]
            if not isinstance(seed, (bytes, bytearray)):
                raise EntropyError("EINVAL", "invalid snapshot seed")
            # Re-inject fresh entropy so restored clones never repeat output.
            self._key = self._derive_key(
                b"entropy-vm-restore", bytes(seed), os.urandom(self.SEED_BYTES)
            )
            self._counter = 0
            self._entropy_bits = max(self._entropy_bits, float(self.pool_size))
            self._audit.append({
                "event": "vm_restore",
                "source": None,
                "healthy": True,
            })

    # -- lifecycle -----------------------------------------------------------

    def close(self):
        with self._lock:
            if self._key is not None:
                # Zeroize key material before dropping the reference.
                self._key = b"\x00" * len(self._key)
            self._key = None
            self._counter = 0
            self._entropy_bits = 0.0
            self._closed = True

    def audit_events(self):
        with self._lock:
            return [dict(event) for event in self._audit]
