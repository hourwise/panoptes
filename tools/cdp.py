"""Minimal Chrome DevTools Protocol client, standard library only.

Launches headless Chrome on an ephemeral port, connects to the browser-level
DevTools WebSocket (hand-rolled: handshake, masked frames, text frames,
ping/pong) and runs commands / collects events on flattened page sessions.

Nothing in here knows about the dashboard; see tools/smoke.py for that.
Always call ``stop()`` (or use ``with``): Chrome dies and the temp profile is
removed on errors and Ctrl-C as well.

    with Chrome() as chrome:
        page = chrome.open_page()
        chrome.navigate(page, "http://127.0.0.1:8765/")
        chrome.cdp.pump(10.0)
        dom = chrome.evaluate(page, "document.title")
        chrome.close_page(page)
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from typing import Any, NamedTuple
from urllib.parse import urlparse
from urllib.request import urlopen

CHROME = "/usr/bin/google-chrome"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_STARTUP_TIMEOUT = 45.0
_CONNECT_TIMEOUT = 10.0
_HTTP_TIMEOUT = 5.0
_COMMAND_TIMEOUT = 30.0


class CDPError(RuntimeError):
    """Protocol, transport or command-level failure."""


class WebSocket:
    """Just enough of RFC 6455 for CDP: client side, masked, text frames."""

    def __init__(self, url: str, timeout: float = _CONNECT_TIMEOUT) -> None:
        parts = urlparse(url)
        if parts.scheme != "ws":
            raise CDPError(f"only ws:// urls are supported, got {url!r}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._sock = socket.create_connection(
                    (host, port), timeout=max(0.1, deadline - time.monotonic())
                )
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise CDPError(
                        f"no DevTools websocket at {host}:{port} within {timeout}s: {exc}"
                    ) from exc
                time.sleep(0.05)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buf = b""
        self._closed = False
        key = base64.b64encode(os.urandom(16)).decode()
        self._handshake(host, port, path, key)

    # -- handshake ----------------------------------------------------------

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            try:
                chunk = self._sock.recv(4096)
            except OSError as exc:  # socket.timeout is an OSError since 3.10
                raise CDPError(f"websocket handshake stalled: {exc}") from exc
            if not chunk:
                raise CDPError("connection closed during handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _handshake(self, host: str, port: int, path: str, key: str) -> None:
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode("latin-1"))
        head = self._read_until(b"\r\n\r\n").decode("latin-1")
        lines = head.split("\r\n")
        if " 101" not in lines[0]:
            raise CDPError(f"websocket handshake failed: {lines[0]!r}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("latin-1")).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise CDPError("websocket handshake: wrong Sec-WebSocket-Accept")

    # -- frames -------------------------------------------------------------

    def _read_exactly(self, n: int) -> bytes:
        """`n` bytes, with the caller's socket timeout as the only deadline.

        A timeout is how `recv` polls, so it passes through untouched. A dead
        socket, or an EOF in the middle of a frame, is a named failure instead
        of a loop that spins on empty reads forever.
        """
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError:
                raise
            except OSError as exc:
                raise CDPError(f"the DevTools websocket died mid-frame: {exc}") from exc
            if not chunk:
                raise CDPError("the DevTools websocket closed mid-frame")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes(header) + mask + masked

    def send_text(self, text: str) -> None:
        if self._closed:
            raise CDPError("websocket is closed")
        self._sock.sendall(self._frame(0x1, text.encode("utf-8")))

    def ping(self, payload: bytes = b"") -> None:
        self._sock.sendall(self._frame(0x9, payload))

    def recv(self, timeout: float | None) -> str | None:
        """Next text message, or None if `timeout` seconds pass without one.

        A timeout — even one landing in the middle of a frame — is a poll
        result, not a failure: what has been read stays buffered and the next
        call resumes from it. Only a dead or closed socket is an error.
        """
        self._sock.settimeout(timeout)
        try:
            return self._recv_message()
        except TimeoutError:
            return None

    def _recv_message(self) -> str | None:
        """Frames until a text message is complete: pings answered, pongs skipped."""
        message: bytes | None = None
        while True:
            head = self._read_exactly(2)
            first, second = head[0], head[1]
            fin, opcode = first & 0x80, first & 0x0F
            masked = bool(second & 0x80)
            n = second & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exactly(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exactly(8))[0]
            mask = self._read_exactly(4) if masked else None
            payload = self._read_exactly(n) if n else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping -> pong
                self._sock.sendall(self._frame(0xA, payload))
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                self._closed = True
                return None
            if opcode in (0x0, 0x1, 0x2):
                message = (message or b"") + payload
                if not fin:
                    continue
                return message.decode("utf-8", "replace")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.sendall(self._frame(0x8, b""))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class Page(NamedTuple):
    target_id: str
    session_id: str


class CDP:
    """Commands and events on one WebSocket; sessions are flattened."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._next_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._names: dict[int, str] = {}
        self.command_timeout = _COMMAND_TIMEOUT
        self.events: list[dict[str, Any]] = []

    def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> int:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        self._names[self._next_id] = method
        try:
            self.ws.send_text(json.dumps(message))
        except OSError as exc:
            raise CDPError(f"{method}: the DevTools socket died: {exc}") from exc
        return self._next_id

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message:
            self._pending[message["id"]] = message
        else:
            self.events.append(message)

    def result(self, msg_id: int, timeout: float | None = None) -> dict[str, Any]:
        """The reply to `msg_id`, with a bounded wait and a named error."""
        if timeout is None:
            timeout = self.command_timeout
        name = self._names.get(msg_id, f"command {msg_id}")
        deadline = time.monotonic() + timeout
        while msg_id not in self._pending:
            left = deadline - time.monotonic()
            if left <= 0:
                self._names.pop(msg_id, None)
                raise CDPError(f"{name}: no reply within {timeout}s (Chrome stuck or gone)")
            text = self.ws.recv(left)
            if text is None:
                continue
            self._dispatch(json.loads(text))
        message = self._pending.pop(msg_id)
        self._names.pop(msg_id, None)
        if "error" in message:
            raise CDPError(f"{name}: {message['error']}")
        return message.get("result", {})

    def call(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self.result(self.send(method, params, session_id), timeout)

    def pump(self, duration: float) -> None:
        """Read whatever arrives during `duration` seconds into `.events`."""
        deadline = time.monotonic() + duration
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            text = self.ws.recv(min(left, 0.2))
            if text is not None:
                self._dispatch(json.loads(text))

    def events_for(self, session_id: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("sessionId") == session_id]

    def clear_events(self) -> None:
        self.events.clear()


class Chrome:
    """Headless Chrome plus its browser-level CDP connection."""

    def __init__(
        self,
        window: str = "1700,1100",
        chrome: str = CHROME,
        startup_timeout: float = _STARTUP_TIMEOUT,
        http_timeout: float = _HTTP_TIMEOUT,
    ) -> None:
        self._chrome = chrome
        self._window = window
        self._startup_timeout = startup_timeout
        self._http_timeout = http_timeout
        self._proc: subprocess.Popen | None = None
        self._profile: str | None = None
        self.cdp: CDP | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "Chrome":
        if self._proc is not None:
            raise CDPError("chrome already running")
        self._profile = tempfile.mkdtemp(prefix="wsd-chrome-")
        args = [
            self._chrome,
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            f"--window-size={self._window}",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
        ]
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(self.stop)
        try:
            port = self._wait_for_port()
            self.cdp = CDP(WebSocket(self._browser_ws_url(port)))
        except BaseException:
            self.stop()
            raise
        return self

    def _wait_for_port(self) -> int:
        marker = os.path.join(self._profile or "", "DevToolsActivePort")
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise CDPError(f"chrome exited early with status {self._proc.returncode}")
            try:
                with open(marker, encoding="ascii") as handle:
                    first = handle.readline().strip()
                if first:
                    return int(first)
            except (OSError, ValueError):
                time.sleep(0.05)
        raise CDPError(
            f"Chrome wrote no DevToolsActivePort in {self._profile} "
            f"within {self._startup_timeout}s"
        )

    def _browser_ws_url(self, port: int) -> str:
        """The browser endpoint from /json/version, retried until it answers."""
        deadline = time.monotonic() + self._http_timeout
        last: Exception = OSError("never reached")
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CDPError(
                    f"Chrome's /json/version did not answer on port {port} "
                    f"within {self._http_timeout}s ({last})"
                )
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=min(2.0, left)
                ) as response:
                    return json.load(response)["webSocketDebuggerUrl"]
            except (OSError, KeyError, ValueError) as exc:
                last = exc
                time.sleep(0.1)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        profile, self._profile = self._profile, None
        atexit.unregister(self.stop)
        if self.cdp is not None:
            self.cdp.ws.close()
            self.cdp = None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)

    def __enter__(self) -> "Chrome":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- pages --------------------------------------------------------------

    def open_page(self) -> Page:
        if self.cdp is None:
            raise CDPError("chrome is not running")
        target = self.cdp.call("Target.createTarget", {"url": "about:blank"})
        target_id = target["targetId"]
        session = self.cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = session["sessionId"]
        for domain in ("Page", "Runtime", "Network"):
            self.cdp.call(f"{domain}.enable", session_id=session_id)
        return Page(target_id, session_id)

    def close_page(self, page: Page) -> None:
        if self.cdp is None:
            return
        try:
            self.cdp.call("Target.closeTarget", {"targetId": page.target_id})
        except CDPError:
            pass

    def navigate(self, page: Page, url: str) -> None:
        assert self.cdp is not None
        self.cdp.call("Page.navigate", {"url": url}, session_id=page.session_id)

    def evaluate(self, page: Page, expression: str) -> Any:
        assert self.cdp is not None
        result = self.cdp.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=page.session_id,
        )
        if result.get("exceptionDetails"):
            raise CDPError(f"evaluate failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")
