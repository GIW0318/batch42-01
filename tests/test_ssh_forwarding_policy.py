import pytest

from ssh_forwarding_policy import ForwardPolicyError, ForwardPolicyGate
import ssh_forwarding_policy


def make_policy(**overrides):
    policy = {
        "allow_hosts": [],
        "deny_hosts": [],
        "allow_ports": [443],
        "allow_dynamic": False,
        "allow_remote_bind": ["127.0.0.1"],
        "allow_unix": [],
        "allow_agent": False,
        "max_per_user": 500,
        "admin_group": "admin",
        "deny_loopback": False,
    }
    policy.update(overrides)
    return policy


def test_allowed_local_forward_opens_channel():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"]))
    channel = gate.open_channel("alice", "local", "10.0.0.5", 443)
    assert channel["id"]
    assert gate.channel_count("alice") == 1
    assert gate.open_fd_count() == 1


def test_dynamic_forward_is_blocked_when_disabled():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"], allow_dynamic=False))
    with pytest.raises(ForwardPolicyError) as exc:
        gate.open_channel("alice", "dynamic", None, None)
    assert exc.value.code == "SSH_OPEN_ADMINISTRATIVELY_PROHIBITED"


def test_remote_forward_wildcard_bind_is_rejected():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"]))
    with pytest.raises(ForwardPolicyError) as exc:
        gate.open_channel(
            "alice", "remote", "10.0.0.5", 443, bind_addr="0.0.0.0"
        )
    assert exc.value.code == "SSH_OPEN_ADMINISTRATIVELY_PROHIBITED"


def test_loopback_variants_are_normalized_and_denied():
    gate = ForwardPolicyGate(
        make_policy(
            allow_hosts=["127.0.0.1", "0.0.0.0", "::1", "127.1"],
            deny_hosts=["127.0.0.1"],
        )
    )
    for host in ("127.0.0.1", "127.1", "0.0.0.0", "::1"):
        with pytest.raises(ForwardPolicyError):
            gate.open_channel("alice", "local", host, 443)


def test_hot_update_closes_existing_violating_forward():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"]))
    channel = gate.open_channel("alice", "local", "10.0.0.5", 443)
    closed = gate.reload_policy(make_policy(allow_hosts=["10.0.0.6"]))
    assert channel["id"] in closed
    with pytest.raises(ForwardPolicyError) as exc:
        gate.data(channel["id"], b"payload")
    assert exc.value.code == "CHANNEL_CLOSED"


def test_per_user_forward_limit_is_exact():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"], max_per_user=2))
    gate.open_channel("alice", "local", "10.0.0.5", 443)
    gate.open_channel("alice", "local", "10.0.0.5", 443)
    with pytest.raises(ForwardPolicyError) as exc:
        gate.open_channel("alice", "local", "10.0.0.5", 443)
    assert exc.value.code == "ERESOURCE"
    assert gate.channel_count("alice") == 2


def test_denied_channel_open_does_not_leak_fd():
    gate = ForwardPolicyGate(make_policy(allow_hosts=[]))
    for _ in range(25):
        with pytest.raises(ForwardPolicyError):
            gate.open_channel("alice", "local", "10.0.0.5", 443)
    assert gate.open_fd_count() == 0
    assert gate.channel_count("alice") == 0


def test_dns_rebinding_is_rechecked_at_connect_time():
    gate = ForwardPolicyGate(
        make_policy(allow_hosts=["10.0.0.5"], deny_loopback=True)
    )
    addresses = ["10.0.0.5", "127.0.0.1"]

    def resolver(host):
        if addresses:
            return addresses.pop(0)
        return "127.0.0.1"

    with pytest.raises(ForwardPolicyError) as exc:
        gate.open_channel(
            "alice", "local", "rebind.example", 443, resolver=resolver
        )
    assert exc.value.code == "SSH_OPEN_ADMINISTRATIVELY_PROHIBITED"
    assert gate.open_fd_count() == 0


def test_unix_socket_forward_is_policy_checked():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"]))
    with pytest.raises(ForwardPolicyError) as exc:
        gate.open_channel(
            "alice", "local", None, None, socket_path="/run/secret.sock"
        )
    assert exc.value.code == "SSH_OPEN_ADMINISTRATIVELY_PROHIBITED"


def test_admin_group_exemption_is_case_sensitive():
    gate = ForwardPolicyGate(
        make_policy(allow_hosts=[], admin_group="admin")
    )
    with pytest.raises(ForwardPolicyError):
        gate.open_channel(
            "alice", "local", "10.0.0.5", 443, groups=("Admin",)
        )
    channel = gate.open_channel(
        "alice", "local", "10.0.0.5", 443, groups=("admin",)
    )
    assert channel["id"]


def test_audit_records_host_and_port():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"]))
    gate.open_channel("alice", "local", "10.0.0.5", 8443)
    events = gate.audit_events()
    assert any(
        event["user"] == "alice"
        and event["host"] == "10.0.0.5"
        and event["port"] == 8443
        for event in events
    )


def test_agent_forwarding_combined_with_port_forward_is_denied():
    gate = ForwardPolicyGate(
        make_policy(allow_hosts=["10.0.0.5"], allow_agent=False)
    )
    with pytest.raises(ForwardPolicyError) as exc:
        gate.open_channel("alice", "agent", "10.0.0.5", 443)
    assert exc.value.code == "SSH_OPEN_ADMINISTRATIVELY_PROHIBITED"


def test_restored_sessions_are_revalidated_against_new_policy():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"]))
    channel = gate.open_channel("alice", "local", "10.0.0.5", 443)
    saved = gate.snapshot_sessions()
    restored = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.6"]))
    closed = restored.restore_sessions(saved)
    assert closed == [channel["id"]]
    with pytest.raises(ForwardPolicyError) as exc:
        restored.data(channel["id"], b"payload")
    assert exc.value.code == "CHANNEL_CLOSED"


def test_close_channel_releases_fd():
    gate = ForwardPolicyGate(make_policy(allow_hosts=["10.0.0.5"]))
    first = gate.open_channel("alice", "local", "10.0.0.5", 443)
    second = gate.open_channel("bob", "local", "10.0.0.5", 443)
    assert gate.open_fd_count() == 2
    gate.close_channel(first["id"])
    assert gate.open_fd_count() == 1
    assert gate.channel_count("alice") == 0
    assert gate.channel_count("bob") == 1
    gate.close_channel(second["id"])
    assert gate.open_fd_count() == 0


def test_remote_forward_with_loopback_bind_to_allowed_target():
    gate = ForwardPolicyGate(
        make_policy(allow_hosts=["10.0.0.5"], allow_remote_bind=["127.0.0.1"])
    )
    channel = gate.open_channel(
        "alice", "remote", "10.0.0.5", 443, bind_addr="127.0.0.1"
    )
    assert channel["id"]
    assert gate.open_fd_count() == 1
