"""M7-T05: netcheck medians against a local socket server (no network)."""
import socket
import threading

import pytest

from duplexbench.netcheck import netcheck, parse, probe


@pytest.fixture
def server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    stop = threading.Event()
    seen = []

    def run():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            with c:
                c.settimeout(0.5)
                try:
                    data = c.recv(1024)
                except OSError:
                    data = b""
                if data:
                    seen.append(data)
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    yield srv.getsockname()[1], seen
    stop.set()
    t.join(2)
    srv.close()


def test_parse():
    assert parse("api.example.com:443") == ("api.example.com", 443, False, "/")
    assert parse("https://api.example.com/v1") == ("api.example.com", 443, True, "/v1")
    assert parse("wss://x.io") == ("x.io", 443, True, "/")
    assert parse("http://x.io:8080") == ("x.io", 8080, False, "/")


def test_probe_local_server(server):
    port, seen = server
    r = probe(f"127.0.0.1:{port}", samples=20)
    assert r["samples"] == 20 and r["n_ok"] == 20 and r["n_errors"] == 0
    assert r["tcp_connect_ms"] is not None and r["tcp_connect_ms"] >= 0
    assert r["round_trip_ms"] is not None and r["round_trip_ms"] >= 0
    assert r["tls"] is False and r["tls_handshake_ms"] is None
    assert len(seen) == 20 and seen[0].startswith(b"HEAD / HTTP/1.1")


def test_netcheck_reports_errors_for_closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                  # nothing listens here now
    out = netcheck([f"127.0.0.1:{port}"], samples=2, timeout=0.5)
    r = out[f"127.0.0.1:{port}"]
    assert r["n_ok"] == 0 and r["n_errors"] == 2 and r["tcp_connect_ms"] is None
