"""Network baseline before a run (spec §4.4 "Network baseline", §10 item 1; BUILD_PLAN M7-T05).

For each endpoint: the median of `samples` (default 20) TCP connects, TLS
handshakes (TLS endpoints only) and light round trips (an HTTP `HEAD /` on a
fresh connection, time to the first response byte). Timed with
`time.perf_counter_ns()` (spec §1.1). The dict goes into the run card.

Endpoints: `host:port`, `tcp://host:port`, `tls://host:port`, or an
`http(s)://` / `ws(s)://` URL (https/wss/tls -> TLS; default ports 80/443).
"""
from __future__ import annotations

import socket
import ssl
import statistics
import time
from typing import Iterable
from urllib.parse import urlsplit

SAMPLES = 20             # spec §4.4: median of 20
TIMEOUT_S = 5.0
_TLS = {"https", "wss", "tls"}


def parse(endpoint: str) -> tuple[str, int, bool, str]:
    """-> (host, port, tls, path)."""
    if "://" not in endpoint:
        endpoint = "tcp://" + endpoint
    u = urlsplit(endpoint)
    tls = u.scheme in _TLS
    port = u.port or (443 if tls else 80)
    return u.hostname or "", port, tls, u.path or "/"


def _ms(t0: int) -> float:
    return (time.perf_counter_ns() - t0) / 1e6


def _connect(host: str, port: int, timeout: float) -> tuple[socket.socket, float]:
    t0 = time.perf_counter_ns()
    s = socket.create_connection((host, port), timeout=timeout)
    return s, _ms(t0)


def probe(endpoint: str, samples: int = SAMPLES, timeout: float = TIMEOUT_S,
          ssl_context: ssl.SSLContext | None = None) -> dict:
    host, port, tls, path = parse(endpoint)
    ctx = ssl_context or (ssl.create_default_context() if tls else None)
    tcp, hs, rt, errors = [], [], [], []
    for _ in range(samples):
        try:
            s, ms = _connect(host, port, timeout)
            tcp.append(ms)
            s.close()
            if tls:
                s, _ = _connect(host, port, timeout)
                t0 = time.perf_counter_ns()
                s = ctx.wrap_socket(s, server_hostname=host)
                hs.append(_ms(t0))
                s.close()
            s, _ = _connect(host, port, timeout)
            if tls:
                s = ctx.wrap_socket(s, server_hostname=host)
            t0 = time.perf_counter_ns()
            s.sendall(f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            if not s.recv(1):
                raise ConnectionError("closed before the first response byte")
            rt.append(_ms(t0))
            s.close()
        except (OSError, ssl.SSLError) as e:
            errors.append(f"{type(e).__name__}: {e}")
    med = lambda xs: round(statistics.median(xs), 3) if xs else None  # noqa: E731
    return {"endpoint": endpoint, "host": host, "port": port, "tls": tls,
            "samples": samples, "tcp_connect_ms": med(tcp),
            "tls_handshake_ms": med(hs) if tls else None, "round_trip_ms": med(rt),
            "n_ok": len(rt), "errors": errors[:3], "n_errors": len(errors)}


def netcheck(endpoints: Iterable[str], samples: int = SAMPLES, timeout: float = TIMEOUT_S,
             ssl_context: ssl.SSLContext | None = None) -> dict:
    """{endpoint: probe(endpoint)} for the run card's `network_baseline`."""
    return {e: probe(e, samples, timeout, ssl_context) for e in endpoints}
