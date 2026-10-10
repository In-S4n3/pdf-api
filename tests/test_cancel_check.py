"""«Cancelar» stops the job: Cloud Run never passes a client hang-up to the
container over HTTP/1.1, so TudoPDF names a URL in X-Cancel-Check and pdf-api
asks it while a tool runs. An abandoned 25-page OCR held one of two instances.

The orphaned child is reaped by tini, the image's PID 1 (as in production).
"""

import json
import os
import socket
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.services import pdf_tools
from tests.test_audit_fixes import _scan

# A stand-in for ocrmypdf: notes its temp dir, waits on a child of its own (as
# ocrmypdf waits on tesseract), then writes a result with an invisible text layer.
FAKE_OCRMYPDF = """#!/bin/sh
echo $$ > "$MARK/leader"
echo "$TMPDIR" > "$MARK/tmpdir"
sleep "$FAKE_SECONDS" &
echo $! > "$MARK/child"
wait
for last; do :; done
python -c "import sys, pymupdf
d = pymupdf.open()
d.new_page().insert_text((72, 72), 'Annual accounts', render_mode=3)
d.save(sys.argv[1])" "$last"
"""
KEY = "test-key"
_needs_proc = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="reads /proc (Linux, Docker)"
)


def _alive(pid: int) -> bool:
    """A zombie counts: an unreaped leader is a leak too."""
    return Path(f"/proc/{pid}").exists()


def _wait(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _cancel_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if "cancel" in t.name.lower()]


def _sockets() -> int:
    """Open sockets in this process."""
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        with suppress(OSError):  # the listing's own fd is gone by now
            count += os.readlink(f"/proc/self/fd/{fd}").startswith("socket:")
    return count


class Stub:
    """TudoPDF's cancel route: answers `mode` and records each request's key."""

    def __init__(self) -> None:
        self.mode = "running"
        self.location = ""
        self.keys: list[str | None] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stub.keys.append(self.headers.get("X-API-Key"))
                if stub.mode.isdigit():  # a redirect status
                    self.send_response(int(stub.mode))
                    self.send_header("Location", stub.location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if stub.mode == "trickle":  # every read inside the timeout, the whole far past it
                    self.send_response(200)
                    self.send_header("Content-Length", "19")
                    self.end_headers()
                    try:
                        for byte in b'{"cancelled": true}':
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.5)
                    except OSError:
                        pass
                    return
                if stub.mode == "reflect":  # a broken proxy echoing the request, key first
                    self.wfile.write(f"X-API-Key: {stub.keys[-1]}\r\n{self.headers}".encode())
                    return
                if stub.mode == "slow":
                    time.sleep(3)  # twice pdf-api's 1.5 s deadline
                status, body = {
                    "running": (200, {"cancelled": False}),
                    "cancelled": (200, {"cancelled": True}),
                    "error": (500, {"cancelled": True}),
                    "bad_json": (200, ["cancelled"]),
                }.get(stub.mode, (200, {"cancelled": False}))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/cancel/job1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.server.shutdown()


@pytest.fixture
def target():
    """Where a redirect points: must never hear from pdf-api."""
    server = Stub()
    yield server
    server.server.shutdown()


@pytest.fixture
def fake_ocr(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "ocrmypdf"
    fake.parent.mkdir()
    fake.write_text(FAKE_OCRMYPDF)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("MARK", str(tmp_path))
    monkeypatch.setenv("API_KEY", KEY)
    return tmp_path


def _post_ocr(client, cancel_url: str):
    return client.post(
        "/v2/ocr",
        files={"file": ("a.pdf", _scan(["Annual accounts approved"]), "application/pdf")},
        headers={"X-API-Key": KEY, "X-Cancel-Check": cancel_url},
    )


@_needs_proc
def test_cancel_kills_the_tool_and_removes_its_files(client, stub, fake_ocr, monkeypatch):
    monkeypatch.setenv("FAKE_SECONDS", "60")
    monkeypatch.setattr(pdf_tools, "_cancel_check_allowed", lambda url: True, raising=False)
    with ThreadPoolExecutor(1) as pool:
        response = pool.submit(_post_ocr, client, stub.url)
        assert _wait(lambda: stub.keys, 10), "pdf-api never asked whether to cancel"
        leader = int((fake_ocr / "leader").read_text())
        child = int((fake_ocr / "child").read_text())
        workdir = Path((fake_ocr / "tmpdir").read_text().strip())
        assert _alive(child) and workdir.is_dir()

        stub.mode = "cancelled"  # the user pressed «Cancelar»
        flipped = time.monotonic()
        result = response.result(timeout=10)

    assert time.monotonic() - flipped < 3.5
    assert result.status_code == 499
    assert result.json()["error"]["code"] == "client_closed"
    assert stub.keys[0] == KEY
    assert _wait(lambda: not _alive(leader) and not _alive(child) and not workdir.exists(), 3), (
        f"leader {_alive(leader)}, child {_alive(child)}, temp dir {workdir.exists()}"
    )


@pytest.mark.parametrize("mode", ["error", "slow", "bad_json", "reflect"])
def test_a_failing_cancel_check_never_stops_the_job(
    client, stub, fake_ocr, monkeypatch, caplog, mode
):
    real, resolved = socket.getaddrinfo, []
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: resolved.append(a[0]) or real(*a, **k)
    )
    # A second check starts by ~1.9 s even when the first runs to its 1.5 s
    # deadline; the job lasts 5 s, so a loaded runner has 3 s of headroom.
    monkeypatch.setattr(pdf_tools, "CANCEL_POLL_SECONDS", 0.2)
    monkeypatch.setenv("FAKE_SECONDS", "5")
    monkeypatch.setattr(pdf_tools, "_cancel_check_allowed", lambda url: True, raising=False)
    stub.mode = mode
    result = _post_ocr(client, stub.url)
    assert result.status_code == 200, result.text
    assert len(stub.keys) >= 2  # asked more than once, warned once
    assert sum("cancel check" in r.getMessage() for r in caplog.records) == 1
    assert KEY not in caplog.text  # "reflect" puts it in the status line
    assert resolved == ["127.0.0.1"]  # once per job, not once per check


def test_a_cancel_url_off_the_allowlist_is_never_requested(client, stub, fake_ocr, monkeypatch):
    monkeypatch.setenv("FAKE_SECONDS", "2.5")
    stub.mode = "cancelled"
    result = _post_ocr(client, stub.url)  # http://127.0.0.1: not https, not ours
    assert result.status_code == 200, result.text
    assert stub.keys == []


@pytest.mark.parametrize("host", ["evil.vercel.app", "tudopdf-x-other.vercel.app"])
def test_another_vercel_team_never_gets_a_request(client, fake_ocr, monkeypatch, host):
    """Anyone can deploy to *.vercel.app; only our team's previews hear the key."""
    dialled = []

    def refuse(host, *args, **kwargs):
        dialled.append(host)
        raise OSError("no network in this test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(pdf_tools, "CANCEL_POLL_SECONDS", 0.2)
    monkeypatch.setenv("FAKE_SECONDS", "1")
    result = _post_ocr(client, f"https://{host}/api/cancel/job1")
    assert result.status_code == 200, result.text
    assert dialled == []


@_needs_proc
def test_a_malformed_cancel_url_leaves_no_tool_running(client, fake_ocr, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_SECONDS", "3")
    try:
        result = _post_ocr(client, "https://[job-secret")
    except ValueError as exc:  # what used to escape after Popen, past the kill guard
        result = exc
    assert _wait(lambda: (fake_ocr / "child").exists(), 5)
    leader = int((fake_ocr / "leader").read_text())
    child = int((fake_ocr / "child").read_text())
    assert _wait(lambda: not _alive(leader) and not _alive(child), 1), (
        f"leader {_alive(leader)}, child {_alive(child)}"
    )
    assert getattr(result, "status_code", result) == 200
    assert sum("X-Cancel-Check ignored" in r.getMessage() for r in caplog.records) == 1
    assert "job-secret" not in caplog.text


@_needs_proc
def test_a_failure_after_spawn_still_kills_the_tool(client, stub, fake_ocr, monkeypatch):
    """Every step after Popen sits inside the guard that kills the group."""

    class NoPoller(threading.Thread):
        def start(self) -> None:
            if self._target is pdf_tools._poll_cancel:
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(pdf_tools, "_cancel_check_allowed", lambda url: True, raising=False)
    monkeypatch.setattr(threading, "Thread", NoPoller)
    monkeypatch.setenv("FAKE_SECONDS", "3")
    assert _post_ocr(client, stub.url).status_code == 500
    _wait(lambda: (fake_ocr / "child").exists(), 1)  # a surviving tool has written both by now
    # Killed at once, the tool may not have written its pids at all.
    pids = [
        int(text)
        for name in ("leader", "child")
        if (fake_ocr / name).exists() and (text := (fake_ocr / name).read_text().strip())
    ]
    assert _wait(lambda: not any(map(_alive, pids)), 1), pids


def test_a_trickling_cancel_check_leaves_no_thread_behind(client, stub, fake_ocr, monkeypatch):
    """A socket timeout bounds each read, not the response: 19 bytes at one per
    0.5 s held the poller and its socket ~10 s after the job had ended."""
    monkeypatch.setattr(pdf_tools, "_cancel_check_allowed", lambda url: True, raising=False)
    monkeypatch.setattr(pdf_tools, "CANCEL_POLL_SECONDS", 0.2)
    monkeypatch.setenv("FAKE_SECONDS", "1.5")
    stub.mode = "trickle"
    result = _post_ocr(client, stub.url)
    assert result.status_code == 200, result.text
    assert stub.keys, "pdf-api never asked whether to cancel"
    # The check in flight runs out its 1.5 s deadline, then the poller exits.
    assert _wait(lambda: not _cancel_threads(), 3), _cancel_threads()


@_needs_proc
def test_a_hanging_resolver_never_delays_the_job(client, stub, fake_ocr, monkeypatch):
    """getaddrinfo cannot be cancelled: it runs in the poller, never in the job,
    and once it returns after the job ended, nothing is asked or left open."""
    real, release, asked = socket.getaddrinfo, threading.Event(), threading.Event()

    def hang(*args, **kwargs):
        asked.set()
        release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", hang)
    monkeypatch.setattr(pdf_tools, "_cancel_check_allowed", lambda url: True, raising=False)
    monkeypatch.setattr(pdf_tools, "CANCEL_POLL_SECONDS", 0.2)
    monkeypatch.setenv("FAKE_SECONDS", "1")
    sockets = _sockets()
    started = time.monotonic()
    try:
        result = _post_ocr(client, stub.url)
        took = time.monotonic() - started
        assert asked.is_set() and _cancel_threads(), "the poller should be stuck resolving"
    finally:
        release.set()
    assert result.status_code == 200, result.text
    assert took < 5, took  # a 1 s tool, nothing added
    assert _wait(lambda: not _cancel_threads(), 1), _cancel_threads()
    assert stub.keys == []
    assert _sockets() <= sockets


def test_an_unreachable_address_leaves_time_for_the_next(stub):
    """The 1.5 s deadline is split across the resolved addresses, not 1.5 s each."""
    full = socket.create_server(("127.0.0.1", 0), backlog=0)  # a full accept queue drops SYNs
    filler = [socket.socket() for _ in range(3)]
    for sock in filler:
        sock.setblocking(False)
        sock.connect_ex(full.getsockname())
    try:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", full.getsockname()),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", stub.server.server_address),
        ]
        parts = urllib.parse.urlsplit(stub.url)
        assert pdf_tools._ask_cancelled(parts, addresses, KEY) is False  # stub: running
        assert stub.keys == [KEY]
    finally:
        for sock in [*filler, full]:
            sock.close()


def test_a_stalled_tls_handshake_ends_at_the_deadline():
    """A server that never answers the ClientHello: the check gives up at its
    deadline and the key never leaves, in plaintext or otherwise."""
    silent = socket.create_server(("127.0.0.1", 0))
    received = bytearray()

    def hold() -> None:
        conn, _ = silent.accept()
        conn.settimeout(4)
        with suppress(OSError):
            while chunk := conn.recv(4096):
                received.extend(chunk)
        conn.close()

    server = threading.Thread(target=hold, daemon=True)
    server.start()
    parts = urllib.parse.urlsplit(f"https://localhost:{silent.getsockname()[1]}/api/cancel/1")
    address = (socket.AF_INET, socket.SOCK_STREAM, 0, "", silent.getsockname())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            pdf_tools._ask_cancelled(parts, [address], KEY)
        assert time.monotonic() - started < 2
    finally:
        silent.close()
    server.join(5)
    assert received and KEY.encode() not in received  # a ClientHello, nothing else


def test_a_failed_check_logs_only_the_error_type(monkeypatch, caplog):
    """An exception's text can echo the response, and a response can echo the key."""

    def echo(*args):
        raise ValueError(f"bad status line: X-API-Key: {KEY}")

    monkeypatch.setattr(pdf_tools, "_ask_cancelled", echo)
    monkeypatch.setattr(pdf_tools, "CANCEL_POLL_SECONDS", 0.05)
    done = threading.Event()
    poller = threading.Thread(
        target=pdf_tools._poll_cancel, args=("http://127.0.0.1:9/x", threading.Event(), done)
    )
    poller.start()
    _wait(lambda: "cancel check" in caplog.text, 2)
    done.set()
    poller.join(2)
    assert "cancel check failed, job continues: ValueError" in caplog.text
    assert KEY not in caplog.text


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_never_reaches_its_target(stub, target, monkeypatch, status):
    """Following it would carry X-API-Key to a host the allowlist never saw."""
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setattr(pdf_tools, "CANCEL_POLL_SECONDS", 0.1)
    stub.mode, stub.location = str(status), target.url
    target.mode = "cancelled"
    gone, done = threading.Event(), threading.Event()
    poller = threading.Thread(target=pdf_tools._poll_cancel, args=(stub.url, gone, done))
    poller.start()
    try:
        assert _wait(lambda: len(stub.keys) >= 2, 5)
    finally:
        done.set()
        poller.join(5)
    assert stub.keys[0] == KEY
    assert target.keys == []
    assert not gone.is_set()


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://tudopdf.app/api/cancel/1", True),
        ("https://www.tudopdf.app/api/cancel/1", True),
        # this project's previews: the deployment, and its branch alias
        ("https://tudopdf-gi7e00q8z-ins4n3s-projects.vercel.app/api/cancel/1", True),
        ("https://tudopdf-git-fix-admin-integridade-ins4n3s-projects.vercel.app/x", True),
        ("https://tudo-pdf-git-main.vercel.app/api/cancel/1", False),
        ("https://tudopdf-x-other.vercel.app/x", False),
        ("https://evil.vercel.app/x", False),
        ("https://tudopdf-x.evil-ins4n3s-projects.vercel.app/x", False),
        ("https://ins4n3s-projects.vercel.app/x", False),
        ("https://tudopdf-x-ins4n3s-projects.vercel.app.evil.com/x", False),
        # malformed: refused, never raised
        ("https://[", False),
        ("https://tudopdf.app:99999/x", False),
        ("https://tudopdf.app/a b", False),
        ("https://tudopdf.app/\x01", False),
        ("https://tudopdf.app/é", False),
        ("http://tudopdf.app/api/cancel/1", False),
        ("https://tudopdf.app.evil.com/x", False),
        ("https://eviltudopdf.app/x", False),
        ("https://tudopdf.app@evil.com/x", False),
        ("https://evil.com/.vercel.app", False),
        ("https://vercel.app/x", False),
        ("https://169.254.169.254/x", False),
    ],
)
def test_cancel_check_allowlist(url, allowed):
    assert pdf_tools._cancel_check_allowed(url) is allowed
