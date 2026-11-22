import smb_session_signing as sss


def new_gate(**kwargs):
    return sss.SmbSigningGate(**kwargs)


def open_session(gate, session_id="s1", **kwargs):
    kwargs.setdefault("dialects", ["SMB3_1_1"])
    kwargs.setdefault("algorithms", ["AES-CMAC", "HMAC-SHA256"])
    result = gate.negotiate("client-1", session_id=session_id, **kwargs)
    assert result["status"] == "STATUS_SUCCESS"
    return session_id


def submit(gate, session_id, payload=b"data", channel=0, share="Public", **kwargs):
    signature = gate.sign_request(session_id, channel, payload)
    return gate.submit(
        session_id,
        channel,
        share,
        "/share/file.txt",
        payload,
        signature,
        **kwargs
    )


def test_negotiate_rejects_smb1_and_weak_algorithms():
    gate = new_gate()
    smb1 = gate.negotiate("c1", ["SMB1"], ["AES-CMAC"], session_id="s1")
    assert smb1["status"] == "STATUS_NOT_SUPPORTED"
    assert smb1["reason"] == "SMB1 not supported"
    weak = gate.negotiate(
        "c2", ["SMB3_1_1"], ["HMAC-MD5"], session_id="s2"
    )
    assert weak["status"] == "STATUS_NOT_SUPPORTED"
    assert weak["reason"] == "no supported signing algorithm"
    assert weak["signing"] is False


def test_negotiate_rejects_client_without_signing():
    gate = new_gate(require_signing=True)
    result = gate.negotiate(
        "c1",
        ["SMB3_1_1"],
        ["AES-CMAC"],
        signing=False,
        session_id="s1",
    )
    assert result["status"] == "STATUS_NOT_SUPPORTED"
    assert result["reason"] == "signing required"
    assert result["session_id"] is None


def test_multichannel_signature_uses_channel_key():
    gate = new_gate()
    open_session(gate)
    gate.bind_channel("s1", 1, "channel-key-1")
    result = submit(gate, "s1", channel=1)
    assert result["status"] == "STATUS_SUCCESS"
    assert result["disconnected"] is False


def test_primary_key_signature_fails_on_second_channel():
    gate = new_gate()
    open_session(gate)
    gate.bind_channel("s1", 1, "channel-key-1")
    primary_signature = gate.sign_request("s1", 0, b"move")
    result = gate.submit(
        "s1",
        1,
        "Public",
        "/share/file.txt",
        b"move",
        primary_signature,
    )
    assert result["status"] == "STATUS_ACCESS_DENIED"
    assert result["disconnected"] is True
    assert gate.is_connected("s1") is False


def test_bad_signature_denies_and_disconnects():
    gate = new_gate()
    open_session(gate)
    bad = {"sequence": 0, "nonce": "n-1", "signature": "bogus"}
    result = gate.submit("s1", 0, "Public", "/x", b"data", bad)
    assert result["status"] == "STATUS_ACCESS_DENIED"
    assert result["disconnected"] is True
    assert gate.is_connected("s1") is False


def test_concurrent_request_is_refused_while_one_is_in_flight():
    gate = new_gate()
    open_session(gate)
    first = submit(gate, "s1", hold=True)
    assert first["status"] == "STATUS_SUCCESS"
    second_signature = gate.sign_request("s1", 0, b"second")
    second = gate.submit(
        "s1", 0, "Public", "/x", b"second", second_signature
    )
    assert second["status"] == "STATUS_REQUEST_NOT_ACCEPTED"
    assert second["reason"] == "concurrent request"
    assert gate.release("s1")["released"] is True
    third = submit(gate, "s1", b"third")
    assert third["status"] == "STATUS_SUCCESS"


def test_credit_is_reclaimed_after_signature_failure():
    gate = new_gate()
    open_session(gate)
    assert gate.available_credits("s1") == 8
    ok = submit(gate, "s1")
    assert ok["status"] == "STATUS_SUCCESS"
    assert gate.available_credits("s1") == 7
    bad = {"sequence": 9, "nonce": "bad", "signature": "nope"}
    denied = gate.submit("s1", 0, "Public", "/x", b"data", bad)
    assert denied["status"] == "STATUS_ACCESS_DENIED"
    assert gate.available_credits("s1") == 8


def test_tampered_ciphertext_is_rejected_before_decrypt():
    gate = new_gate()
    open_session(gate)
    original = b"secret-ciphertext"
    signature = gate.sign_request("s1", 0, original)
    result = gate.submit(
        "s1",
        0,
        "Public",
        "/x",
        None,
        signature,
        ciphertext=b"tampered-ciphertext",
        encrypted=True,
    )
    assert result["status"] == "STATUS_ACCESS_DENIED"
    assert result["disconnected"] is True
    assert result.get("data") is None


def test_replayed_negotiate_token_is_rejected():
    gate = new_gate()
    open_session(gate)
    first = gate.renegotiate(
        "s1", "token-1", ["SMB3_1_1"], ["AES-CMAC", "HMAC-SHA256"]
    )
    assert first["status"] == "STATUS_SUCCESS"
    replay = gate.renegotiate(
        "s1", "token-1", ["SMB3_1_1"], ["HMAC-MD5"]
    )
    assert replay["status"] == "STATUS_INVALID_PARAMETER"
    assert replay["reason"] == "replayed negotiate"
    unsigned = gate.submit("s1", 0, "Public", "/x", b"data", None)
    assert unsigned["status"] == "STATUS_ACCESS_DENIED"


def test_expired_session_invalidates_handle_and_lease():
    gate = new_gate()
    open_session(gate, now=0.0, ttl=100.0)
    handle = gate.open_handle(
        "s1", "Public", "/share/file.txt", lease_ttl=100.0, now=0.0
    )
    handle_id = handle["handle_id"]
    assert gate.lease_active(handle_id) is True
    expired = gate.expire_sessions(200.0)
    assert "s1" in expired["expired"]
    assert gate.is_connected("s1") is False
    read = gate.read_handle(handle_id, now=200.0)
    assert read["status"] == "STATUS_FILE_CLOSED"
    assert gate.lease_active(handle_id) is False


def test_restart_restore_handle_requires_original_session_key():
    gate = new_gate()
    open_session(gate)
    handle = gate.open_handle(
        "s1", "Public", "/share/file.txt", durable=True
    )
    handle_id = handle["handle_id"]
    original_key = gate.session_key("s1")
    state = gate.export_state()
    restarted = new_gate()
    restarted.load_state(state)
    denied = restarted.restore_handle(handle_id, "wrong-key")
    assert denied["status"] == "STATUS_ACCESS_DENIED"
    restored = restarted.restore_handle(handle_id, original_key)
    assert restored["status"] == "STATUS_SUCCESS"
    assert restarted.read_handle(handle_id)["status"] == "STATUS_SUCCESS"


def test_share_policy_is_case_insensitive():
    gate = new_gate(
        require_signing=False, share_rules={"Finance": {"require_signing": True}}
    )
    open_session(gate, signing=False)
    protected = gate.submit("s1", 0, "FINANCE", "/x", b"data", None)
    assert protected["status"] == "STATUS_ACCESS_DENIED"
    open_again = gate.negotiate(
        "client-2",
        ["SMB3_1_1"],
        ["AES-CMAC"],
        signing=False,
        session_id="s2",
    )
    assert open_again["status"] == "STATUS_SUCCESS"
    public = gate.submit("s2", 0, "public", "/x", b"data", None)
    assert public["status"] == "STATUS_SUCCESS"


def test_directory_query_over_64k_reports_buffer_overflow():
    gate = new_gate()
    open_session(gate)
    entries = [
        {"name": "file-{:05d}-".format(index) + "x" * 60, "size": index}
        for index in range(1200)
    ]
    handle = gate.open_handle(
        "s1", "Public", "/share/dir", entries=entries
    )
    result = gate.query_directory(handle["handle_id"])
    assert result["status"] == "STATUS_BUFFER_OVERFLOW"
    assert result["truncated"] is True
    assert result["returned_bytes"] <= 64 * 1024
    assert len(result["entries"]) < len(entries)


def test_rename_keeps_handle_valid_and_updates_path():
    gate = new_gate()
    open_session(gate)
    handle = gate.open_handle("s1", "Public", "/share/a.txt")
    handle_id = handle["handle_id"]
    renamed = gate.rename(handle_id, "/share/b.txt")
    assert renamed["status"] == "STATUS_SUCCESS"
    assert gate.handle_path(handle_id) == "/share/b.txt"
    assert gate.read_handle(handle_id)["status"] == "STATUS_SUCCESS"


def test_audit_never_stores_raw_ntlm_response():
    gate = new_gate()
    open_session(gate)
    result = submit(gate, "s1", ntlm_response="SECRET-NTLM-BLOB")
    assert result["status"] == "STATUS_SUCCESS"
    entries = gate.audit_entries()
    assert entries
    dumped = str(entries)
    assert "SECRET-NTLM-BLOB" not in dumped
