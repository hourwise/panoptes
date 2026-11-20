"""HTTP server skeleton for the workstation dashboard."""

import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from . import MILESTONE, PRODUCT, __version__

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

TOKEN = secrets.token_urlsafe(32)  # per-process; injected only into "/"

ROUTES: dict[str, Callable[[], object]] = {}
POST_ROUTES: dict[str, Callable[[dict], object]] = {}
# prefix -> handler(the rest of the path), for paths with an id in the middle
# (``/api/engines/<id>/profiles``, ``/api/jobs/<id>/report.md``) which cannot be
# enumerated up front. Registered with :func:`route_prefix`.
PREFIX_ROUTES: dict[str, Callable[[str], object]] = {}
MAX_POST_BODY = 4 * 1024


class RouteError(Exception):
    """A handler's deliberate HTTP answer: ``raise RouteError(404, "...")``."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    """A handler's answer when the body is not JSON: Markdown, text, bytes.

    Returned from a :func:`route` or :func:`route_prefix` handler instead of a
    JSON-serialisable object. ``status`` and ``headers`` go out as given; a
    ``str`` body is encoded as UTF-8. Served on GET and HEAD only, with
    ``no-store`` like every other API path.
    """

    body: bytes | str
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)
    status: int = 200


def route(path: str) -> Callable[[Callable[[], object]], Callable[[], object]]:
    """Register a GET handler; returns JSON data or a :class:`Response`."""

    def decorator(func: Callable[[], object]) -> Callable[[], object]:
        ROUTES[path] = func
        return func

    return decorator


def route_prefix(prefix: str) -> Callable[[Callable[[str], object]], Callable[[str], object]]:
    """Register a GET handler for every path starting with ``prefix``.

    The handler gets the remainder of the path (never empty) and answers with
    JSON data, a :class:`Response`, or a :class:`RouteError` for anything it
    does not serve. The longest registered prefix wins, so a more specific
    prefix can take a whole subtree away from a shorter one.
    """

    def decorator(func: Callable[[str], object]) -> Callable[[str], object]:
        PREFIX_ROUTES[prefix] = func
        return func

    return decorator


def _match_prefix(path: str) -> tuple[Callable[[str], object], str] | None:
    """Longest registered prefix covering ``path``, with the remainder."""
    for prefix in sorted(PREFIX_ROUTES, key=len, reverse=True):
        if path.startswith(prefix) and len(path) > len(prefix):
            return PREFIX_ROUTES[prefix], path[len(prefix):]
    return None


def post_route(path: str) -> Callable[[Callable[[dict], object]], Callable[[dict], object]]:
    """Register a POST handler receiving the parsed JSON body."""

    def decorator(func: Callable[[dict], object]) -> Callable[[dict], object]:
        POST_ROUTES[path] = func
        return func

    return decorator


@route("/api/health")
def _health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "product": PRODUCT,
        "milestone": MILESTONE,
    }


def _resolve_static(url_path: str) -> Path | None:
    """Resolve /static/<name> or / inside WEB_DIR, or None if outside."""
    rel = url_path[len("/static/"):] if url_path.startswith("/static/") else url_path.lstrip("/")
    if not rel:
        return None
    candidate = (WEB_DIR / unquote(rel)).resolve()
    if candidate == WEB_DIR or WEB_DIR not in candidate.parents:
        return None
    return candidate


def _etag(info: os.stat_result) -> str:
    """Strong validator for a static file: size + mtime, so a rewrite changes it."""
    return f'"{info.st_size:x}-{info.st_mtime_ns:x}"'


def _etag_matches(header: str | None, etag: str) -> bool:
    """True when an If-None-Match list (strong comparison, "*" allowed) covers etag."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    return any(tag.strip().removeprefix("W/") == etag for tag in header.split(","))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        cache: str = "no-store",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_not_modified(self, etag: str) -> None:
        """304 carries headers only: no body, no Content-Length."""
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _send_json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _serve_static(self, path: Path) -> None:
        """Static files are revalidated on every load: ``no-cache`` plus an
        ETag, so an edited CSS/JS file is refetched instead of served stale."""
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        etag = _etag(path.stat())
        if _etag_matches(self.headers.get("If-None-Match"), etag):
            self._send_not_modified(etag)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), content_type, cache="no-cache", headers={"ETag": etag})

    def _serve_index(self) -> None:
        """Serve "/" with ``{{WSD_TOKEN}}`` replaced by the per-process token.

        Deliberately no ETag and no 304: the body depends on the token, so a
        cached copy could carry a stale one. ``no-store`` goes further than
        ``no-cache`` and keeps browsers from restoring the page out of the
        back/forward cache, where its token would already be dead.
        """
        index = WEB_DIR / "index.html"
        if not index.is_file():
            self._send_json(404, {"error": "not found"})
            return
        html = index.read_bytes().replace(b"{{WSD_TOKEN}}", TOKEN.encode("ascii"))
        self._send(200, html, "text/html; charset=utf-8", cache="no-store")

    def _call(self, invoke: Callable[[], object]) -> None:
        """Run a GET handler: a RouteError is its own answer, anything else a 500."""
        try:
            payload = invoke()
        except RouteError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            self._send_json(500, {"error": type(exc).__name__})
        else:
            self._send_payload(payload)

    def _send_payload(self, payload: object) -> None:
        """A Response keeps its own status, content type and headers; else JSON."""
        if isinstance(payload, Response):
            body = payload.body if isinstance(payload.body, bytes) else payload.body.encode("utf-8")
            self._send(payload.status, body, payload.content_type, headers=payload.headers)
        else:
            self._send_json(200, payload)

    def _dispatch(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._serve_index()
        elif path.startswith("/static/"):
            resolved = _resolve_static(path)
            if resolved is None:
                self._send_json(404, {"error": "not found"})
            else:
                self._serve_static(resolved)
        elif path == "/favicon.ico":
            self._serve_static(WEB_DIR / "favicon.svg")
        elif path in ROUTES:
            self._call(ROUTES[path])
        elif (matched := _match_prefix(path)) is not None:
            handler, tail = matched
            self._call(lambda: handler(tail))
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def _method_not_allowed(self) -> None:
        self._send_json(405, {"error": "method not allowed"})

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    def _post_check(self) -> str | None:
        """First failed browser protection, or None when the POST may proceed.

        Host pins the request to this loopback origin (DNS rebinding), Origin
        must be the page's own (cross-site forms/scripts), Content-Type must
        force a CORS preflight this server never answers, and the unguessable
        per-process token blocks anything that got neither header right.
        """
        host = self.headers.get("Host")
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"[::1]:{port}", f"localhost:{port}"):
            return "bad host"
        if self.headers.get("Origin") != f"http://{host}":
            return "bad origin"
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return "bad content-type"
        if not hmac.compare_digest(self.headers.get("X-WSD-Token") or "", TOKEN):
            return "bad token"
        return None

    def do_POST(self) -> None:
        reason = self._post_check()
        if reason is not None:
            self.close_connection = True  # body unread: never keep this connection alive
            self._send_json(403, {"error": reason})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            self._send_json(400, {"error": "bad content-length"})
            return
        if not 0 <= length <= MAX_POST_BODY:
            self.close_connection = True
            self._send_json(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            self._send_json(400, {"error": "invalid json"})
            return
        handler = POST_ROUTES.get(urlparse(self.path).path)
        if handler is None:
            self._send_json(405, {"error": "method not allowed"})
            return
        try:
            payload = handler(body)
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            self._send_json(500, {"error": type(exc).__name__})
        else:
            self._send_json(200, payload)


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create the dashboard server; host must be a loopback address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"not a loopback address: {host!r}") from None
    if not addr.is_loopback:
        raise ValueError(f"not a loopback address: {host!r}")
    server_cls: type[ThreadingHTTPServer] = ThreadingHTTPServer
    if addr.version == 6:
        class V6ThreadingHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_cls = V6ThreadingHTTPServer
    return server_cls((host, port), Handler)
