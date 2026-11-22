import threading

import pytest

import coap_blockwise_assembler as cba


TOKEN = "tok-1"
RESOURCE = "/firmware/meta"


def new_assembler(**kwargs):
    return cba.CoapBlockAssembler(**kwargs)


def put(assembler, block_num, more, payload, block_size=4, etag="v1", token=TOKEN,
        resource=RESOURCE, now=0.0):
    return assembler.put_block(
        token,
        resource,
        block_num,
        more,
        payload,
        block_size=block_size,
        etag=etag,
        now=now,
    )


def test_first_block_returns_continue():
    assembler = new_assembler()
    result = put(assembler, 0, True, b"abcd")
    assert result["code"] == "2.31 Continue"
    assert result["complete"] is False
    assert result["duplicate"] is False
    assert result["expected_block"] == 1
    assert len(assembler.pending_uploads()) == 1
    assert assembler.buffered_bytes() == 4


def test_sequential_blocks_complete_with_exact_body():
    assembler = new_assembler()
    assert put(assembler, 0, True, b"abcd")["code"] == "2.31 Continue"
    assert put(assembler, 1, True, b"efgh")["code"] == "2.31 Continue"
    result = put(assembler, 2, False, b"ijkl")
    assert result["code"] == "2.04 Changed"
    assert result["complete"] is True
    assert result["body"] == b"abcdefghijkl"
    assert assembler.pending_uploads() == []
    assert assembler.buffered_bytes() == 0


def test_gap_returns_request_entity_incomplete_not_continue():
    assembler = new_assembler()
    put(assembler, 0, True, b"abcd")
    gap = put(assembler, 2, True, b"ijkl")
    assert gap["code"] == "4.08 Request Entity Incomplete"
    assert gap["expected_block"] == 1
    assert gap["complete"] is False
    assert assembler.buffered_bytes() == 4
    put(assembler, 1, True, b"efgh")
    done = put(assembler, 2, False, b"ijkl")
    assert done["complete"] is True
    assert done["body"] == b"abcdefghijkl"


def test_duplicate_block_is_not_appended_twice():
    assembler = new_assembler()
    put(assembler, 0, True, b"abcd")
    duplicate = put(assembler, 0, True, b"abcd")
    assert duplicate["duplicate"] is True
    assert duplicate["code"] == "2.31 Continue"
    assert assembler.buffered_bytes() == 4
    done = put(assembler, 1, False, b"efgh")
    assert done["complete"] is True
    assert done["body"] == b"abcdefgh"


def test_block_size_change_discards_assembly():
    assembler = new_assembler()
    put(assembler, 0, True, b"abcdefgh", block_size=8)
    changed = put(assembler, 1, True, b"ijkl", block_size=4)
    assert changed["code"] == "4.08 Request Entity Incomplete"
    assert changed["reason"] == "block size changed"
    assert assembler.pending_uploads() == []
    assert assembler.buffered_bytes() == 0


def test_etag_change_discards_whole_assembly():
    assembler = new_assembler()
    put(assembler, 0, True, b"abcd", etag="v1")
    changed = put(assembler, 1, True, b"efgh", etag="v2")
    assert changed["code"] == "4.08 Request Entity Incomplete"
    assert changed["reason"] == "etag changed"
    assert assembler.pending_uploads() == []
    fresh = put(assembler, 0, True, b"WXYZ", etag="v2")
    assert fresh["code"] == "2.31 Continue"


def test_same_token_concurrent_upload_conflicts():
    assembler = new_assembler()
    first = put(assembler, 0, True, b"AAAA", etag="alpha")
    assert first["code"] == "2.31 Continue"
    conflict = put(assembler, 0, True, b"BBBB", etag="beta")
    assert conflict["code"] == "4.09 Conflict"
    assert conflict["complete"] is False
    assert len(assembler.pending_uploads()) == 1
    assert assembler.buffered_bytes() == 4
    done = put(assembler, 1, False, b"CCCC", etag="alpha")
    assert done["complete"] is True
    assert done["body"] == b"AAAACCCC"


def test_total_size_cap_returns_entity_too_large():
    assembler = new_assembler(max_total_bytes=10)
    assert put(assembler, 0, True, b"abcd")["code"] == "2.31 Continue"
    assert put(assembler, 1, True, b"efgh")["code"] == "2.31 Continue"
    over = put(assembler, 2, False, b"ijkl")
    assert over["code"] == "4.13 Request Entity Too Large"
    assert over["reason"] == "entity too large"
    assert over["complete"] is False
    assert assembler.pending_uploads() == []
    assert assembler.buffered_bytes() == 0


def test_oversized_block_size_returns_size_hint():
    assembler = new_assembler(max_block_size=1024)
    result = put(assembler, 0, True, b"x", block_size=2048)
    assert result["code"] == "4.13 Request Entity Too Large"
    assert result["reason"] == "block size too large"
    assert result["szx"] == 1024
    assert assembler.pending_uploads() == []


def test_block_number_beyond_16_bits_raises():
    assembler = new_assembler()
    with pytest.raises(cba.BlockNumberOutOfRange):
        put(assembler, 70000, False, b"x")
    assert assembler.pending_uploads() == []


def test_single_block_upload_without_more_completes_immediately():
    assembler = new_assembler()
    result = put(assembler, 0, False, b"tiny")
    assert result["code"] == "2.04 Changed"
    assert result["complete"] is True
    assert result["body"] == b"tiny"
    assert assembler.pending_uploads() == []


def test_expire_frees_idle_buffers():
    assembler = new_assembler(timeout=30.0)
    put(assembler, 0, True, b"abcd", now=0.0)
    assert assembler.buffered_bytes() == 4
    result = assembler.expire(100.0)
    assert result["expired"] == 1
    assert assembler.pending_uploads() == []
    assert assembler.buffered_bytes() == 0


def test_restart_resumes_partial_upload():
    assembler = new_assembler()
    put(assembler, 0, True, b"abcd")
    put(assembler, 1, True, b"efgh")
    state = assembler.export_state()
    restarted = new_assembler()
    restarted.load_state(state)
    assert restarted.buffered_bytes() == 8
    done = put(restarted, 2, False, b"ijkl")
    assert done["complete"] is True
    assert done["body"] == b"abcdefghijkl"
    assert restarted.pending_uploads() == []


def test_final_block_ahead_of_window_is_incomplete():
    assembler = new_assembler()
    put(assembler, 0, True, b"abcd")
    ahead = put(assembler, 2, False, b"ijkl")
    assert ahead["code"] == "4.08 Request Entity Incomplete"
    assert ahead["expected_block"] == 1
    assert ahead["complete"] is False
    put(assembler, 1, True, b"efgh")
    done = put(assembler, 2, False, b"ijkl")
    assert done["complete"] is True
    assert done["body"] == b"abcdefghijkl"


def test_two_tokens_assemble_independently_under_threads():
    assembler = new_assembler()
    barrier = threading.Barrier(2)
    bodies = {}
    errors = []

    def upload(token, first, second):
        try:
            barrier.wait()
            put(assembler, 0, True, first, token=token, resource="/r")
            result = put(assembler, 1, False, second, token=token, resource="/r")
            bodies[token] = result["body"]
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=upload, args=("t1", b"AAAA", b"BBBB")),
        threading.Thread(target=upload, args=("t2", b"CCCC", b"DDDD")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == []
    assert bodies == {"t1": b"AAAABBBB", "t2": b"CCCCDDDD"}
    assert assembler.pending_uploads() == []
