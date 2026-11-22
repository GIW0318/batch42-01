"""IPsec policy/SA/selector synchronized lookup table."""

import copy
import ipaddress
import re
import threading


class IpsecError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(code if not message else "%s: %s" % (code, message))


_DIRECTIONS = ("out", "in")
_NO_SA_ACTIONS = frozenset(("discard", "bypass", "none", "pass", "clear", "allow"))
_MIN_DH_GROUP = 14
_REPLAY_WINDOW = 256
_MODP_BITS_TO_GROUP = {
    768: 1,
    1024: 2,
    1536: 5,
    2048: 14,
    3072: 15,
    4096: 16,
    6144: 17,
    8192: 18,
}


def _error(code, message=""):
    return IpsecError(code, message)


def _as_int(value, what):
    if isinstance(value, bool):
        raise _error("EINVAL", "invalid %s" % what)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _error("EINVAL", "invalid %s" % what)


def _as_spi(value):
    if isinstance(value, str):
        text = value.strip().lower()
        base = 16 if text.startswith("0x") else 10
        try:
            spi = int(text, base)
        except ValueError:
            raise _error("EINVAL", "invalid SPI")
    else:
        spi = _as_int(value, "SPI")
    if spi < 0 or spi > 0xFFFFFFFF:
        raise _error("EINVAL", "SPI out of range")
    return spi


def _network(selector):
    if isinstance(selector, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return selector
    if isinstance(selector, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return ipaddress.ip_network(selector)
    if isinstance(selector, bytes):
        selector = selector.decode("ascii")
    if not isinstance(selector, str):
        raise _error("EINVAL", "invalid selector")
    text = selector.strip()
    if not text:
        raise _error("EINVAL", "empty selector")
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError:
        raise _error("EINVAL", "invalid selector: %s" % selector)


def _address(value):
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return value
    if not isinstance(value, str):
        raise _error("EINVAL", "invalid address")
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        raise _error("EINVAL", "invalid address: %s" % value)


def _directions(value):
    if value is None:
        return frozenset(("out",))
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("both", "inout", "in/out", "out/in", "any", "*"):
            return frozenset(_DIRECTIONS)
        if text in _DIRECTIONS:
            return frozenset((text,))
    elif isinstance(value, (set, frozenset, list, tuple)):
        result = set()
        for item in value:
            result.update(_directions(item))
        if result:
            return frozenset(result)
    raise _error("EINVAL", "invalid direction")


def _lookup_direction(value):
    if value is None:
        return ("out",)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _DIRECTIONS:
            return (text,)
        if text in ("both", "inout", "in/out", "out/in", "any", "*"):
            return _DIRECTIONS
    raise _error("EINVAL", "invalid direction")


def _dh_group_number(value):
    """Return the numeric DH group for an explicit group value."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _error("NO_PROPOSAL_CHOSEN", "invalid DH group")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise _error("NO_PROPOSAL_CHOSEN", "invalid DH group")
    text = value.strip().lower()
    if not text:
        raise _error("NO_PROPOSAL_CHOSEN", "invalid DH group")
    if text.isdigit():
        return int(text)
    match = re.search(r"\bmodp\s*(\d+)\b", text)
    if match:
        bits = int(match.group(1))
        if bits in _MODP_BITS_TO_GROUP:
            return _MODP_BITS_TO_GROUP[bits]
        return _MIN_DH_GROUP if bits >= 2048 else 0
    match = re.search(r"\b(?:dh|group)[-_ ]*(\d+)\b", text)
    if match:
        return int(match.group(1))
    raise _error("NO_PROPOSAL_CHOSEN", "unsupported DH group")


def _is_weak_group(group):
    return group is None or group < _MIN_DH_GROUP


def _proposal_dh_group(proposal):
    if proposal is None:
        return None
    if isinstance(proposal, dict):
        for key in ("dh_group", "dh-group", "dhgroup", "dh", "group", "pfs_group"):
            if key in proposal:
                return _dh_group_number(proposal[key])
        return None
    if isinstance(proposal, (list, tuple)):
        for item in proposal:
            group = _proposal_dh_group(item)
            if group is not None:
                return group
        return None
    if not isinstance(proposal, str):
        return None
    text = proposal.strip().lower()
    match = re.search(r"\bmodp\s*(\d+)\b", text)
    if match:
        bits = int(match.group(1))
        return _MODP_BITS_TO_GROUP.get(bits, _MIN_DH_GROUP if bits >= 2048 else 0)
    match = re.search(r"\b(?:dh|group)[-_ ]*(\d+)\b", text)
    if match:
        return int(match.group(1))
    return None


def _validate_proposal(proposal):
    if proposal is None:
        return
    group = _proposal_dh_group(proposal)
    if group is not None and _is_weak_group(group):
        raise _error("NO_PROPOSAL_CHOSEN", "weak DH group")


def _validate_dh_group(dh_group):
    if dh_group is None:
        return _MIN_DH_GROUP
    group = _dh_group_number(dh_group)
    if _is_weak_group(group):
        raise _error("NO_PROPOSAL_CHOSEN", "weak DH group")
    return group


class _Policy:
    __slots__ = ("policy_id", "network", "action", "priority", "proposal", "directions")

    def __init__(self, policy_id, network, action, priority, proposal, directions):
        self.policy_id = policy_id
        self.network = network
        self.action = action
        self.priority = priority
        self.proposal = copy.deepcopy(proposal)
        self.directions = frozenset(directions)

    @property
    def needs_sa(self):
        return str(self.action).lower() not in _NO_SA_ACTIONS

    def to_snapshot(self):
        return {
            "id": self.policy_id,
            "selector": str(self.network),
            "action": self.action,
            "priority": self.priority,
            "proposal": copy.deepcopy(self.proposal),
            "directions": [d for d in _DIRECTIONS if d in self.directions],
        }


class _SA:
    __slots__ = (
        "spi",
        "policy_id",
        "enc",
        "dh_group",
        "port",
        "replay_highest",
        "replay_seen",
    )

    def __init__(
        self,
        spi,
        policy_id,
        enc,
        dh_group,
        port,
        replay_highest=0,
        replay_seen=None,
    ):
        self.spi = spi
        self.policy_id = policy_id
        self.enc = enc
        self.dh_group = dh_group
        self.port = port
        self.replay_highest = replay_highest
        self.replay_seen = set(replay_seen or ())

    def public(self):
        return {
            "spi": self.spi,
            "policy_id": self.policy_id,
            "enc": self.enc,
            "dh_group": self.dh_group,
            "port": self.port,
        }

    def to_snapshot(self):
        return {
            "spi": self.spi,
            "policy_id": self.policy_id,
            "enc": self.enc,
            "dh_group": self.dh_group,
            "port": self.port,
            "replay_highest": self.replay_highest,
            "replay_seen": sorted(self.replay_seen),
        }


class IpsecPolicyTable:
    def __init__(self):
        self._lock = threading.RLock()
        self._policies = {}
        self._index = {}
        self._sas_by_policy = {}
        self._sas_by_spi = {}
        self._next_policy_id = 1

    # ------------------------------------------------------------------
    # Policy and selector synchronization
    # ------------------------------------------------------------------
    def _index_key(self, network, direction):
        return (network.version, direction, network.prefixlen)

    @staticmethod
    def _network_index_value(network):
        return int(network.network_address) >> (
            network.max_prefixlen - network.prefixlen
        )

    def _index_add_locked(self, policy):
        value = self._network_index_value(policy.network)
        for direction in policy.directions:
            key = self._index_key(policy.network, direction)
            bucket = self._index.setdefault(key, {})
            bucket.setdefault(value, []).append(policy.policy_id)

    def _index_remove_locked(self, policy):
        value = self._network_index_value(policy.network)
        for direction in policy.directions:
            key = self._index_key(policy.network, direction)
            bucket = self._index.get(key)
            if bucket is None:
                continue
            entries = bucket.get(value)
            if entries is None:
                continue
            try:
                entries.remove(policy.policy_id)
            except ValueError:
                pass
            if not entries:
                bucket.pop(value, None)
            if not bucket:
                self._index.pop(key, None)

    def _add_policy_locked(self, selector, action, priority, proposal, directions):
        network = _network(selector)
        action = str(action)
        priority = _as_int(priority, "priority")
        directions = frozenset(directions)
        _validate_proposal(proposal)

        for existing in self._policies.values():
            if existing.priority != priority:
                continue
            if not (existing.directions & directions):
                continue
            if existing.network.version != network.version:
                continue
            if existing.network.overlaps(network):
                raise _error(
                    "EEXIST",
                    "overlapping selector with the same priority",
                )

        policy_id = self._next_policy_id
        self._next_policy_id += 1
        policy = _Policy(policy_id, network, action, priority, proposal, directions)
        self._policies[policy_id] = policy
        self._index_add_locked(policy)
        return policy_id

    def add_policy(self, selector, action="encrypt", priority=100,
                   proposal=None, direction="out"):
        with self._lock:
            return self._add_policy_locked(
                selector, action, priority, proposal, _directions(direction)
            )

    def add_policy_pair(self, selector, action="encrypt", priority=100, proposal=None):
        with self._lock:
            return self._add_policy_locked(
                selector, action, priority, proposal, frozenset(_DIRECTIONS)
            )

    def validate_bidirectional(self, policy_id):
        with self._lock:
            policy = self._policies.get(policy_id)
            if policy is None:
                raise _error("NO_POLICY", "policy not found")
            if set(_DIRECTIONS).issubset(policy.directions):
                return True
            opposite = "in" if "out" in policy.directions else "out"
            for other in self._policies.values():
                if other is policy:
                    continue
                if opposite not in other.directions:
                    continue
                if other.network == policy.network:
                    return True
            raise _error("ONE_WAY_POLICY", "missing reverse direction policy")

    def delete_policy(self, policy_id):
        with self._lock:
            policy = self._policies.pop(policy_id, None)
            if policy is None:
                raise _error("NO_POLICY", "policy not found")
            self._index_remove_locked(policy)
            sa = self._sas_by_policy.pop(policy_id, None)
            if sa is not None:
                current = self._sas_by_spi.get(sa.spi)
                if current is sa:
                    self._sas_by_spi.pop(sa.spi, None)
            return True

    def lookup(self, src, dst, proto="tcp", sport=0, dport=0, direction="out"):
        with self._lock:
            src_address = _address(src)
            dst_address = _address(dst)
            search_directions = _lookup_direction(direction)

            best = None
            best_rank = None
            for search_direction in search_directions:
                address = src_address
                bits = address.max_prefixlen
                value = int(address)
                for prefixlen in range(bits, -1, -1):
                    key = (address.version, search_direction, prefixlen)
                    bucket = self._index.get(key)
                    if not bucket:
                        continue
                    network_value = value >> (bits - prefixlen)
                    policy_ids = bucket.get(network_value)
                    if not policy_ids:
                        continue
                    for policy_id in policy_ids:
                        policy = self._policies[policy_id]
                        rank = (prefixlen, -policy.priority, -policy_id)
                        if best_rank is None or rank > best_rank:
                            best_rank = rank
                            best = policy_id
                    break

            if best is None:
                raise _error("NO_POLICY", "no matching policy")
            return best

    # ------------------------------------------------------------------
    # SAD synchronization
    # ------------------------------------------------------------------
    def _remove_sa_locked(self, sa):
        if self._sas_by_policy.get(sa.policy_id) is sa:
            self._sas_by_policy.pop(sa.policy_id, None)
        if self._sas_by_spi.get(sa.spi) is sa:
            self._sas_by_spi.pop(sa.spi, None)

    def install_sa(self, policy_id, spi, enc="aes256", dh_group=14, port=500):
        with self._lock:
            if policy_id not in self._policies:
                raise _error("NO_POLICY", "policy not found")
            spi = _as_spi(spi)
            group = _validate_dh_group(dh_group)
            port = _as_int(port, "port")
            if port < 0 or port > 65535:
                raise _error("EINVAL", "port out of range")

            conflict = self._sas_by_spi.get(spi)
            if conflict is not None and conflict.policy_id != policy_id:
                raise _error("EEXIST", "SPI already in use")

            old = self._sas_by_policy.get(policy_id)
            if old is not None:
                self._remove_sa_locked(old)

            sa = _SA(spi, policy_id, str(enc), group, port)
            self._sas_by_policy[policy_id] = sa
            self._sas_by_spi[spi] = sa
            return sa.public()

    def update_proposal(self, policy_id, proposal):
        with self._lock:
            policy = self._policies.get(policy_id)
            if policy is None:
                raise _error("NO_POLICY", "policy not found")
            _validate_proposal(proposal)
            sa = self._sas_by_policy.pop(policy_id, None)
            if sa is not None:
                self._remove_sa_locked(sa)
            policy.proposal = copy.deepcopy(proposal)
            return True

    def select_sa(self, policy_id):
        with self._lock:
            sa = self._sas_by_policy.get(policy_id)
            if sa is None:
                raise _error("NO_SA", "no SA for policy")
            return sa.public()

    def sa_for_port(self, policy_id, port):
        with self._lock:
            sa = self._sas_by_policy.get(policy_id)
            if sa is None:
                return None
            try:
                wanted = _as_int(port, "port")
            except IpsecError:
                return None
            return sa.public() if sa.port == wanted else None

    def list_sa_spis(self, policy_id):
        with self._lock:
            sa = self._sas_by_policy.get(policy_id)
            return [] if sa is None else [sa.spi]

    def expire_sa(self, spi):
        with self._lock:
            spi = _as_spi(spi)
            sa = self._sas_by_spi.get(spi)
            if sa is None:
                return False
            self._remove_sa_locked(sa)
            return True

    def check_replay(self, spi, seq):
        with self._lock:
            spi = _as_spi(spi)
            sa = self._sas_by_spi.get(spi)
            if sa is None:
                raise _error("NO_SA", "SA not found")
            seq = _as_int(seq, "sequence number")
            if seq <= 0:
                return False

            if seq > sa.replay_highest:
                sa.replay_highest = seq
                cutoff = seq - _REPLAY_WINDOW
                if cutoff > 0:
                    sa.replay_seen = {value for value in sa.replay_seen if value > cutoff}
                sa.replay_seen.add(seq)
                return True

            if sa.replay_highest - seq >= _REPLAY_WINDOW:
                return False
            if seq in sa.replay_seen:
                return False
            sa.replay_seen.add(seq)
            return True

    def rekey(self, policy_id, new_spi, enc="aes256"):
        with self._lock:
            if policy_id not in self._policies:
                raise _error("NO_POLICY", "policy not found")
            new_spi = _as_spi(new_spi)

            old = self._sas_by_policy.get(policy_id)
            conflict = self._sas_by_spi.get(new_spi)
            if conflict is not None and conflict is not old:
                raise _error("EEXIST", "SPI already in use")

            dh_group = old.dh_group if old is not None else _MIN_DH_GROUP
            port = old.port if old is not None else 500
            sa = _SA(new_spi, policy_id, str(enc), dh_group, port)

            # Publish the replacement before withdrawing the old SA. Both
            # operations happen under the same lock so there is no gap.
            self._sas_by_policy[policy_id] = sa
            self._sas_by_spi[new_spi] = sa
            if old is not None and old.spi != new_spi:
                self._sas_by_spi.pop(old.spi, None)
            return sa.public()

    # ------------------------------------------------------------------
    # Counters, health and restart
    # ------------------------------------------------------------------
    def policy_count(self):
        with self._lock:
            return len(self._policies)

    def sa_count(self):
        with self._lock:
            return len(self._sas_by_spi)

    def health(self):
        with self._lock:
            missing_sa = []
            for policy_id, policy in sorted(self._policies.items()):
                if policy.needs_sa and policy_id not in self._sas_by_policy:
                    missing_sa.append(policy_id)

            orphan_sa = []
            for spi, sa in sorted(self._sas_by_spi.items()):
                if sa.policy_id not in self._policies:
                    orphan_sa.append(spi)

            return {
                "ok": not missing_sa and not orphan_sa,
                "missing_sa": missing_sa,
                "orphan_sa": orphan_sa,
                "policy_count": len(self._policies),
                "sa_count": len(self._sas_by_spi),
            }

    def snapshot(self):
        with self._lock:
            return {
                "version": 1,
                "next_policy_id": self._next_policy_id,
                "policies": [
                    policy.to_snapshot()
                    for _, policy in sorted(self._policies.items())
                ],
                "sas": [
                    sa.to_snapshot() for _, sa in sorted(self._sas_by_spi.items())
                ],
            }

    def restore(self, snapshot):
        if not isinstance(snapshot, dict):
            raise _error("EINVAL", "invalid snapshot")

        policies = {}
        index = {}
        sas_by_policy = {}
        sas_by_spi = {}

        raw_policies = snapshot.get("policies", ())
        if not isinstance(raw_policies, (list, tuple)):
            raise _error("EINVAL", "invalid policy snapshot")
        for record in raw_policies:
            if not isinstance(record, dict):
                raise _error("EINVAL", "invalid policy record")
            try:
                policy_id = _as_int(record["id"], "policy id")
                selector = record.get("selector", record.get("network"))
                network = _network(selector)
                action = str(record.get("action", "encrypt"))
                priority = _as_int(record.get("priority", 100), "priority")
                proposal = copy.deepcopy(record.get("proposal"))
                directions = _directions(
                    record.get("directions", record.get("direction", "out"))
                )
            except KeyError:
                raise _error("EINVAL", "incomplete policy record")
            if policy_id in policies:
                raise _error("EINVAL", "duplicate policy id")
            _validate_proposal(proposal)
            policy = _Policy(policy_id, network, action, priority, proposal, directions)
            policies[policy_id] = policy
            value = self._network_index_value(network)
            for direction in directions:
                key = (network.version, direction, network.prefixlen)
                bucket = index.setdefault(key, {})
                bucket.setdefault(value, []).append(policy_id)

        raw_sas = snapshot.get("sas", ())
        if not isinstance(raw_sas, (list, tuple)):
            raise _error("EINVAL", "invalid SA snapshot")
        for record in raw_sas:
            if not isinstance(record, dict):
                raise _error("EINVAL", "invalid SA record")
            try:
                spi = _as_spi(record["spi"])
                policy_id = _as_int(record["policy_id"], "policy id")
            except KeyError:
                raise _error("EINVAL", "incomplete SA record")
            if spi in sas_by_spi:
                raise _error("EINVAL", "duplicate SPI")
            group = _validate_dh_group(record.get("dh_group", _MIN_DH_GROUP))
            port = _as_int(record.get("port", 500), "port")
            if port < 0 or port > 65535:
                raise _error("EINVAL", "port out of range")
            highest = _as_int(record.get("replay_highest", 0), "replay state")
            seen = record.get("replay_seen", ())
            if not isinstance(seen, (list, tuple, set, frozenset)):
                raise _error("EINVAL", "invalid replay state")
            seen = {_as_int(value, "replay state") for value in seen}
            sa = _SA(
                spi,
                policy_id,
                str(record.get("enc", "aes256")),
                group,
                port,
                highest,
                seen,
            )
            sas_by_spi[spi] = sa
            if policy_id in sas_by_policy:
                raise _error("EINVAL", "multiple SAs for one policy")
            sas_by_policy[policy_id] = sa

        next_policy_id = _as_int(
            snapshot.get("next_policy_id", 1), "next policy id"
        )
        if policies:
            next_policy_id = max(next_policy_id, max(policies) + 1)
        else:
            next_policy_id = max(next_policy_id, 1)

        with self._lock:
            self._policies = policies
            self._index = index
            self._sas_by_policy = sas_by_policy
            self._sas_by_spi = sas_by_spi
            self._next_policy_id = next_policy_id
        return True
