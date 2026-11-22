import threading

import pytest

import ftp_bounce_arbiter
from ftp_bounce_arbiter import FtpBounceArbiter, FtpError


def _arb(**kwargs):
    options = dict(passive_ports=range(50000, 50010), advertised_host="203.0.113.10")
    options.update(kwargs)
    arb = FtpBounceArbiter(**options)
    arb.open_control("c1", "198.51.100.1", 40001)
    return arb


def test_port_to_foreign_host_rejected():
    arb = _arb()
    with pytest.raises(FtpError) as exc:
        arb.handle_port("c1", "203.0.113.77", 40002)
    assert exc.value.code == "bounce"


def test_port_to_control_peer_accepted():
    arb = _arb()
    target = arb.handle_port("c1", "198.51.100.1", 40002)
    assert target["host"] == "198.51.100.1"
    assert target["port"] == 40002
    assert arb.initiator("c1") == "server"


def test_pasv_uses_advertised_public_host():
    arb = _arb()
    reply = arb.handle_pasv("c1")
    assert reply["host"] == "203.0.113.10"
    assert 50000 <= reply["port"] < 50010
    assert reply["initiator"] == "client"


def test_epsv_after_port_switches_initiator_to_client():
    arb = _arb()
    arb.handle_port("c1", "198.51.100.1", 40002)
    reply = arb.handle_epsv("c1")
    assert arb.initiator("c1") == "client"
    assert 50000 <= reply["port"] < 50010


def test_passive_ports_unique_across_connections():
    arb = _arb()
    arb.open_control("c2", "198.51.100.2", 40002)
    first = arb.handle_pasv("c1")["port"]
    second = arb.handle_pasv("c2")["port"]
    assert first != second
    assert {first, second} <= set(range(50000, 50010))


def test_passive_port_pool_exhaustion_error():
    arb = _arb(passive_ports=range(50000, 50001))
    arb.open_control("c2", "198.51.100.2", 40002)
    arb.handle_pasv("c1")
    with pytest.raises(FtpError) as exc:
        arb.handle_pasv("c2")
    assert exc.value.code == "no-passive-port"


def test_data_source_mismatch_rejected():
    arb = _arb()
    arb.handle_pasv("c1")
    with pytest.raises(FtpError) as exc:
        arb.open_data("c1", "203.0.113.77", 40003)
    assert exc.value.code == "data-source-mismatch"


def test_data_source_match_accepted():
    arb = _arb()
    arb.handle_pasv("c1")
    assert arb.open_data("c1", "198.51.100.1", 40003)["connected"] is True
    assert arb.active_data_connections() == 1


def test_half_open_data_expires():
    arb = _arb(passive_timeout=30)
    arb.handle_pasv("c1")
    arb.open_data("c1", "198.51.100.1", 40003)
    arb.advance(31)
    expired = arb.expire_data()
    assert expired == ["c1"]
    assert arb.active_data_connections() == 0


def test_control_close_releases_ports_and_data():
    arb = _arb()
    port = arb.handle_pasv("c1")["port"]
    arb.open_data("c1", "198.51.100.1", 40003)
    released = arb.close_control("c1")
    assert port in released
    assert arb.active_data_connections() == 0
    assert arb.reserved_ports() == []


def test_rest_offset_beyond_eof_rejected():
    arb = _arb()
    with pytest.raises(FtpError) as exc:
        arb.validate_rest("c1", 101, 100)
    assert exc.value.code == "invalid-rest"
    assert arb.validate_rest("c1", 100, 100) == 100


def test_rest_negative_rejected():
    arb = _arb()
    with pytest.raises(FtpError) as exc:
        arb.validate_rest("c1", -1, 100)
    assert exc.value.code == "invalid-rest"


def test_stor_path_traversal_rejected():
    arb = _arb()
    with pytest.raises(FtpError) as exc:
        arb.resolve_stor_path("c1", "anonymous", "../../etc/passwd")
    assert exc.value.code == "path-traversal"
    with pytest.raises(FtpError) as exc:
        arb.resolve_stor_path("c1", "anonymous", "/etc/passwd")
    assert exc.value.code == "path-traversal"


def test_anonymous_cannot_write_system_dir():
    arb = _arb()
    path = arb.resolve_stor_path("c1", "anonymous", "file.txt")
    assert path == "/srv/ftp/pub/file.txt"
    assert arb.allow_write("c1", "anonymous", path) is True
    assert arb.allow_write("c1", "anonymous", "/etc/passwd") is False


def test_concurrent_passive_allocation_unique():
    arb = _arb(passive_ports=range(60000, 60004))
    for number in range(4):
        arb.open_control("cc%d" % number, "198.51.100.%d" % (number + 10), 40001)
    barrier = threading.Barrier(4)
    ports = []
    lock = threading.Lock()

    def worker(conn_id):
        barrier.wait()
        port = arb.allocate_passive_port(conn_id)
        with lock:
            ports.append(port)

    threads = [threading.Thread(target=worker, args=("cc%d" % n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ports) == 4
    assert len(set(ports)) == 4
