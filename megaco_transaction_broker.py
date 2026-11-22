"""H.248/Megaco transaction broker skeleton."""

import copy
import threading

UNKNOWN_CONTEXT = "unknown-context"
INVALID_ACTION = "invalid-action"
TX_BUSY = "tx-busy"
ENCODING_MISMATCH = "encoding-mismatch"
TERMINATION_LIMIT = "termination-limit"
UNKNOWN_TERMINATION = "unknown-termination"

_TEXT_HEADERS = {1, "1", "text"}
_BINARY_HEADERS = {2, "2", "binary"}


class MegacoError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


class MegacoTransactionBroker:
    """Transaction-id idempotency over Context/Termination state.

    A transaction's Actions apply all together or not at all.  Termination
    ids are normalized to lower case with numeric prefixes stripped of
    leading zeros.  Encoding is decided from the message header.
    """

    def __init__(self, max_terminations=1000):
        self.max_terminations = max_terminations
        self._lock = threading.RLock()
        self._contexts = {}
        self._in_flight = set()
        self._completed = {}
        self._plays = {}
        self._context_seq = 0

    def create_context(self, context_id=None):
        with self._lock:
            if context_id is None:
                self._context_seq += 1
                context_id = "ctx-%d" % self._context_seq
            if context_id not in self._contexts:
                self._contexts[context_id] = {}
            return context_id

    def delete_context(self, context_id):
        with self._lock:
            if context_id not in self._contexts:
                raise MegacoError(UNKNOWN_CONTEXT, context_id)
            del self._contexts[context_id]
            return True

    def normalize_termination_id(self, termination_id):
        parts = []
        for part in str(termination_id).split("/"):
            part = part.strip().lower()
            if part.isdigit():
                part = part.lstrip("0") or "0"
            parts.append(part)
        return "/".join(parts)

    def add_termination(self, context_id, termination_id, emergency=False):
        with self._lock:
            context = self._require_context(context_id)
            normalized = self.normalize_termination_id(termination_id)
            if normalized in context:
                if emergency:
                    context[normalized]["emergency"] = True
                return normalized
            if self.termination_count_total() >= self.max_terminations:
                raise MegacoError(TERMINATION_LIMIT, normalized)
            context[normalized] = {"emergency": bool(emergency)}
            return normalized

    def termination_count_total(self):
        return sum(len(terms) for terms in self._contexts.values())

    def _require_context(self, context_id):
        if context_id not in self._contexts:
            raise MegacoError(UNKNOWN_CONTEXT, context_id)
        return self._contexts[context_id]

    def termination_info(self, context_id, termination_id):
        with self._lock:
            context = self._require_context(context_id)
            normalized = self.normalize_termination_id(termination_id)
            if normalized not in context:
                raise MegacoError(UNKNOWN_TERMINATION, normalized)
            info = dict(context[normalized])
            info["context"] = context_id
            info["termination"] = normalized
            return info

    def transaction(self, tx_id, actions, encoding="text"):
        with self._lock:
            if tx_id in self._completed:
                cached = dict(self._completed[tx_id])
                cached["duplicate"] = True
                return cached
            if tx_id in self._in_flight:
                raise MegacoError(TX_BUSY, tx_id)
            backup_contexts = copy.deepcopy(self._contexts)
            backup_plays = copy.deepcopy(self._plays)
            try:
                results = [self._apply_action(action) for action in actions]
            except MegacoError:
                self._contexts = backup_contexts
                self._plays = backup_plays
                raise
            result = {"tx": tx_id, "duplicate": False,
                      "encoding": encoding, "results": results}
            self._completed[tx_id] = result
            return dict(result)

    def _apply_action(self, action):
        kind = action.get("action")
        context_id = action.get("context")
        if kind == "add":
            normalized = self.add_termination(
                context_id, action.get("termination"),
                emergency=action.get("emergency", False))
            return {"action": "add", "context": context_id,
                    "termination": normalized}
        if kind == "play":
            context = self._require_context(context_id)
            normalized = self.normalize_termination_id(action.get("termination"))
            if normalized not in context:
                raise MegacoError(UNKNOWN_TERMINATION, normalized)
            self._plays[normalized] = self._plays.get(normalized, 0) + 1
            return {"action": "play", "context": context_id,
                    "termination": normalized}
        if kind == "subtract":
            context = self._require_context(context_id)
            normalized = self.normalize_termination_id(action.get("termination"))
            if normalized not in context:
                raise MegacoError(UNKNOWN_TERMINATION, normalized)
            del context[normalized]
            return {"action": "subtract", "context": context_id,
                    "termination": normalized}
        raise MegacoError(INVALID_ACTION, str(kind))

    def begin_transaction(self, tx_id):
        with self._lock:
            if tx_id in self._in_flight:
                raise MegacoError(TX_BUSY, tx_id)
            self._in_flight.add(tx_id)
            return tx_id

    def end_transaction(self, tx_id):
        with self._lock:
            self._in_flight.discard(tx_id)
            return tx_id

    def audit_value(self, context_id):
        with self._lock:
            context = self._require_context(context_id)
            return sorted(context)

    def keepalive(self, context_id):
        return {"keepalive": True, "context": context_id}

    def sweep_empty(self):
        with self._lock:
            removed = sorted(context_id for context_id, terms
                             in self._contexts.items() if not terms)
            for context_id in removed:
                del self._contexts[context_id]
            return removed

    def decode_transaction(self, header, payload):
        if header in _TEXT_HEADERS:
            if not isinstance(payload, str):
                raise MegacoError(ENCODING_MISMATCH,
                                  "text header with non-text payload")
            return {"encoding": "text", "payload": payload}
        if header in _BINARY_HEADERS:
            if not isinstance(payload, (bytes, bytearray)):
                raise MegacoError(ENCODING_MISMATCH,
                                  "binary header with non-binary payload")
            return {"encoding": "binary", "payload": bytes(payload)}
        raise MegacoError(ENCODING_MISMATCH, "unknown header %r" % (header,))

    def context_count(self):
        with self._lock:
            return len(self._contexts)

    def termination_count(self, context_id):
        with self._lock:
            return len(self._require_context(context_id))

    def play_count(self, termination_id):
        with self._lock:
            return self._plays.get(
                self.normalize_termination_id(termination_id), 0)

    def re_register(self):
        with self._lock:
            return sorted(self._in_flight)

    def snapshot(self):
        with self._lock:
            return {
                "max_terminations": self.max_terminations,
                "contexts": copy.deepcopy(self._contexts),
                "in_flight": sorted(self._in_flight),
                "completed": copy.deepcopy(self._completed),
                "plays": copy.deepcopy(self._plays),
                "context_seq": self._context_seq,
            }

    def restore(self, snapshot):
        with self._lock:
            self.max_terminations = snapshot.get("max_terminations",
                                                 self.max_terminations)
            self._contexts = copy.deepcopy(snapshot.get("contexts", {}))
            self._in_flight = set(snapshot.get("in_flight", []))
            self._completed = copy.deepcopy(snapshot.get("completed", {}))
            self._plays = copy.deepcopy(snapshot.get("plays", {}))
            self._context_seq = snapshot.get("context_seq", 0)
            return True
