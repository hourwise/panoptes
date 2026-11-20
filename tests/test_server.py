import http.client
import threading
import unittest
import urllib.error
import urllib.request
import json
import shutil
import tempfile
import subprocess
from pathlib import Path
from unittest import mock

import wsd.routes
import wsd.server
from wsd import MILESTONE, PRODUCT, __version__
from wsd.lifecycle.control import ControlClient
from wsd.server import ROUTES, make_server


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as resp:
            return resp.status, dict(resp.headers), resp.read()

    def test_health(self):
        status, headers, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(
            json.loads(body),
            {
                "status": "ok",
                "version": __version__,
                "product": PRODUCT,
                "milestone": MILESTONE,
            },
        )

    def test_index_page(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"PANOPTES", body)

    def test_static_index(self):
        status, _, body = self.get("/static/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"PANOPTES", body)

    def test_token_appears_only_in_the_index_page(self):
        token = wsd.server.TOKEN.encode()
        self.assertEqual(len(wsd.server.TOKEN), 43)  # secrets.token_urlsafe(32)
        _status, _headers, body = self.get("/")
        self.assertIn(token, body)
        self.assertNotIn(b"{{WSD_TOKEN}}", body)  # placeholder fully replaced
        for path in ("/static/index.html", "/static/app.js", "/api/health", "/favicon.ico"):
            with self.subTest(path=path):
                _s, _h, data = self.get(path)
                self.assertNotIn(token, data)
        _s, _h, raw_index = self.get("/static/index.html")
        self.assertIn(b"{{WSD_TOKEN}}", raw_index)  # the raw file keeps the placeholder

    def test_favicon_is_served_as_svg(self):
        status, headers, body = self.get("/favicon.ico")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", body)
        static_status, _, static_body = self.get("/static/favicon.svg")
        self.assertEqual(static_status, 200)
        self.assertEqual(static_body, body)

    def test_static_is_revalidated_with_a_strong_etag(self):
        status, headers, _body = self.get("/static/ui/kit.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertTrue(headers["ETag"].startswith('"'))  # strong: no W/ prefix

    def test_static_conditional_get_returns_304_without_body(self):
        _status, headers, _body = self.get("/static/ui/kit.js")
        etag = headers["ETag"]
        req = urllib.request.Request(self.base + "/static/ui/kit.js", headers={"If-None-Match": etag})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 304)
        self.assertEqual(ctx.exception.read(), b"")
        self.assertEqual(ctx.exception.headers["ETag"], etag)
        self.assertEqual(ctx.exception.headers["Cache-Control"], "no-cache")
        self.assertNotIn("Content-Length", ctx.exception.headers)

    def test_index_page_has_no_etag_and_is_never_304(self):
        """The token lives in the body, so "/" must never come back from a cache
        or a 304: no validator, and no-store so no copy is kept at all."""
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("ETag", headers)
        req = urllib.request.Request(self.base + "/", headers={"If-None-Match": "*"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), body)

    def test_unknown_or_wildcard_validator(self):
        req = urllib.request.Request(self.base + "/static/ui/kit.js", headers={"If-None-Match": '"nope"'})
        with urllib.request.urlopen(req) as resp:  # a stale tag must not suppress the body
            self.assertEqual(resp.status, 200)
            self.assertNotEqual(resp.headers["ETag"], '"nope"')
        req = urllib.request.Request(self.base + "/static/ui/kit.js", headers={"If-None-Match": "*"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 304)

    def test_api_responses_stay_unvalidated_and_unstored(self):
        status, headers, _body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("ETag", headers)

    def test_static_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/../wsd/server.py")
        self.assertEqual(ctx.exception.code, 404)

    def test_static_traversal_encoded(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/%2e%2e/wsd/server.py")
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(json.loads(ctx.exception.read()), {"error": "not found"})

    def test_post_on_get_route_rejected_after_all_checks_pass(self):
        req = urllib.request.Request(
            self.base + "/api/health",
            data=b"{}",
            method="POST",
            headers={
                "Origin": self.base,
                "Content-Type": "application/json",
                "X-WSD-Token": wsd.server.TOKEN,
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 405)
        self.assertEqual(json.loads(ctx.exception.read())["error"], "method not allowed")

    def test_raising_handler(self):
        def boom():
            raise RuntimeError("kaboom")

        ROUTES["/api/boom"] = boom
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get("/api/boom")
            self.assertEqual(ctx.exception.code, 500)
            self.assertEqual(json.loads(ctx.exception.read()), {"error": "RuntimeError"})
            # server keeps running
            status, _, _ = self.get("/api/health")
            self.assertEqual(status, 200)
        finally:
            del ROUTES["/api/boom"]

    def test_head_allowed(self):
        req = urllib.request.Request(self.base + "/api/health", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_response_route_keeps_its_status_and_headers(self):
        ROUTES["/api/notes"] = lambda: wsd.server.Response(
            "# notes\n",
            "text/markdown; charset=utf-8",
            {"Content-Disposition": 'attachment; filename="notes.md"'},
            status=201,
        )
        try:
            status, headers, body = self.get("/api/notes")
        finally:
            del ROUTES["/api/notes"]
        self.assertEqual(status, 201)
        self.assertEqual(headers["Content-Type"], "text/markdown; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="notes.md"')
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, b"# notes\n")

    def test_response_route_head_carries_headers_without_body(self):
        ROUTES["/api/notes"] = lambda: wsd.server.Response(b"bytes body", "text/plain; charset=utf-8")
        try:
            req = urllib.request.Request(self.base + "/api/notes", method="HEAD")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers["Content-Type"], "text/plain; charset=utf-8")
                self.assertEqual(resp.headers["Content-Length"], "10")
                self.assertEqual(resp.read(), b"")
        finally:
            del ROUTES["/api/notes"]

    def test_route_prefix_handler_gets_the_tail(self):
        seen = []

        @wsd.server.route_prefix("/api/thing/")
        def thing(tail: str) -> dict:
            seen.append(tail)
            return {"tail": tail}

        self.addCleanup(wsd.server.PREFIX_ROUTES.pop, "/api/thing/", None)
        status, headers, body = self.get("/api/thing/T-013/report.md")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), {"tail": "T-013/report.md"})
        self.assertEqual(seen, ["T-013/report.md"])

    def test_route_prefix_handler_can_answer_with_a_response(self):
        @wsd.server.route_prefix("/api/blob/")
        def blob(tail: str):
            if tail.endswith(".txt"):
                return wsd.server.Response(tail, "text/plain; charset=utf-8")
            raise wsd.server.RouteError(404, f"no blob {tail!r}")

        self.addCleanup(wsd.server.PREFIX_ROUTES.pop, "/api/blob/", None)
        status, _headers, body = self.get("/api/blob/a.txt")
        self.assertEqual((status, body), (200, b"a.txt"))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/blob/a.bin")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(json.loads(ctx.exception.read())["error"], "no blob 'a.bin'")

    def test_etag_follows_the_file_on_disk(self):
        """A rewritten file gets a new validator, so a stale tag refetches the body."""
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a.js").write_text("one")
        original = wsd.server.WEB_DIR
        try:
            wsd.server.WEB_DIR = tmp
            first = self.get("/static/a.js")[1]["ETag"]
            (tmp / "a.js").write_text("one two")
            self.assertNotEqual(self.get("/static/a.js")[1]["ETag"], first)
            req = urllib.request.Request(self.base + "/static/a.js", headers={"If-None-Match": first})
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), b"one two")
        finally:
            wsd.server.WEB_DIR = original
            shutil.rmtree(tmp)

    def test_non_loopback_host(self):
        for host in ("0.0.0.0", "10.0.0.1", "8.8.8.8", "localhost", "example.invalid"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    make_server(host, 0)

    def test_ipv6_loopback_serves_health(self):
        server = make_server("::1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        url = f"http://[::1]:{port}/api/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(
                json.loads(resp.read()),
                {
                    "status": "ok",
                    "version": __version__,
                    "product": PRODUCT,
                    "milestone": MILESTONE,
                },
            )


class PostProtectionTest(unittest.TestCase):
    """Every browser protection must reject the POST on its own."""

    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.hits = []
        wsd.server.POST_ROUTES["/api/spy"] = self.spy
        self.addCleanup(wsd.server.POST_ROUTES.pop, "/api/spy")

    def spy(self, body):
        self.hits.append(body)
        return {"echo": body}

    def post(self, path="/api/spy", body=b"{}", **overrides):
        headers = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
            "X-WSD-Token": wsd.server.TOKEN,
        }
        for key, value in overrides.items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def test_all_checks_pass_calls_handler_exactly_once(self):
        status, headers, body = self.post(body=b'{"confirm": true}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"echo": {"confirm": True}})
        self.assertEqual(self.hits, [{"confirm": True}])
        self.assertFalse([k for k in headers if k.startswith("access-control-")])

    def test_missing_or_foreign_origin_403(self):
        for origin in (None, "", "http://evil.example", "http://127.0.0.1", "null", f"http://127.0.0.1:{self.port}x"):
            with self.subTest(origin=origin):
                status, _h, body = self.post(Origin=origin)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "bad origin"})
        self.assertEqual(self.hits, [])

    def test_wrong_host_403(self):
        bad_hosts = ("example.com", "localhost", "127.0.0.2:1", f"127.0.0.1:{self.port + 1}", f"evil.example:{self.port}")
        for host in bad_hosts:  # Host: is always sent (http.client injects it), so only wrong values are testable
            with self.subTest(host=host):
                status, _h, body = self.post(Host=host)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "bad host"})
        self.assertEqual(self.hits, [])

    def test_loopback_hosts_and_matching_origins_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"):
            with self.subTest(host=host):
                status, _h, _body = self.post(Host=host, Origin=f"http://{host}")
                self.assertEqual(status, 200)

    def test_wrong_content_type_403(self):
        for ctype in (None, "", "text/plain", "application/x-www-form-urlencoded", "application/jsonx"):
            with self.subTest(content_type=ctype):
                status, _h, body = self.post(**{"Content-Type": ctype})
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "bad content-type"})
        status, _h, _body = self.post(**{"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)  # parameters do not change the media type
        self.assertEqual(len(self.hits), 1)

    def test_missing_or_wrong_token_403(self):
        for token in (None, "", "x" * 43, wsd.server.TOKEN[:-1] + ("A" if wsd.server.TOKEN[-1] != "A" else "B")):
            with self.subTest(token=token):
                status, _h, body = self.post(**{"X-WSD-Token": token})
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "bad token"})
        self.assertEqual(self.hits, [])

    def test_body_over_4kb_rejected(self):
        status, _h, body = self.post(body=b'{"a": "' + b"x" * 5000 + b'"}')
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body), {"error": "body too large"})
        self.assertEqual(self.hits, [])

    def test_invalid_json_400(self):
        status, _h, body = self.post(body=b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "invalid json"})

    def test_unregistered_post_path_still_405(self):
        status, _h, body = self.post(path="/api/health")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body), {"error": "method not allowed"})

    def test_put_delete_patch_options_still_405(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                try:
                    conn.request(method, "/api/spy", body=b"{}")
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 405)
                    self.assertEqual(json.loads(resp.read())["error"], "method not allowed")
                finally:
                    conn.close()


class StubControl:
    def __init__(self):
        self.posts = []

    def status(self):
        return {"ok": True, "op": "status", "state": {"Running": True}}

    def worker_active(self):
        return True

    def logs(self):
        return "one\ntwo"

    def run(self, op):
        self.posts.append(op)
        return {"ok": True, "op": op}


class ControlRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.stub = StubControl()
        self.original = wsd.routes._control
        wsd.routes._control = self.stub
        self.addCleanup(setattr, wsd.routes, "_control", self.original)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return resp.status, json.loads(resp.read())

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", path, body=json.dumps(body), headers={
                "Host": f"127.0.0.1:{self.port}",
                "Origin": f"http://127.0.0.1:{self.port}",
                "Content-Type": "application/json",
                "X-WSD-Token": wsd.server.TOKEN,
            })
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_status_endpoint_shape(self):
        status, payload = self.get("/api/control/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": self.stub.status(), "worker_active": True})

    def test_logs_endpoint_shape(self):
        status, payload = self.get("/api/control/logs")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"lines": ["one", "two"]})

    def test_logs_helper_error_passes_through(self):
        self.stub.logs = lambda: {"ok": False, "error": "logs unavailable"}
        status, payload = self.get("/api/control/logs")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": False, "error": "logs unavailable"})

    def test_lifecycle_post_calls_helper_once_per_op(self):
        for op in ("start", "stop", "restart"):
            with self.subTest(op=op):
                status, payload = self.post(f"/api/control/{op}", {"confirm": True})
                self.assertEqual(status, 200)
                self.assertEqual(payload, {"ok": True, "op": op})
        self.assertEqual(self.stub.posts, ["start", "stop", "restart"])

    def test_post_without_confirmation_never_reaches_the_helper(self):
        for body in ({}, {"confirm": False}, {"confirm": "yes"}, [True], "confirm"):
            with self.subTest(body=body):
                status, payload = self.post("/api/control/restart", body)
                self.assertEqual(status, 200)
                self.assertFalse(payload["ok"])
        self.assertEqual(self.stub.posts, [])

    def test_real_client_posts_exact_argv_once(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '{"ok": true, "op": "stop"}', "")

        wsd.routes._control = ControlClient(runner=fake_run)
        status, payload = self.post("/api/control/stop", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "op": "stop"})
        self.assertEqual(calls, [["sudo", "-n", "/usr/local/sbin/radiance-ctl", "stop"]])

    def test_gets_never_invoke_lifecycle_writes(self):
        self.get("/api/control/status")
        self.get("/api/control/logs")
        self.assertEqual(self.stub.posts, [])


class EngineRoutesTest(unittest.TestCase):
    """GET /api/engines and the read-only profile routes."""

    ACTIVE = "flashnext.yaml"
    OTHER = "flashnext-spec6.yaml"

    class Stub:
        """Just the adapter surface the routes read: ids, connection, snapshot."""

        def __init__(self, engine_id, label, base_url, status="down", model=None, caps=None):
            self.engine_id = engine_id
            self.label = label or engine_id
            self.base_url = base_url.rstrip("/")
            self.status = status
            self.model = model
            self.caps = caps or {}

        def snapshot(self):
            return {
                "id": self.engine_id,
                "type": "radiance",
                "status": self.status,
                "model": self.model,
                "capabilities": self.caps,
            }

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.root.name)
        (cls.dir / cls.ACTIVE).write_text("context: 8192\nretoken: auto\n")
        (cls.dir / cls.OTHER).write_text("context: 4096\nretoken: auto\n")
        (cls.dir / "notes.txt").write_text("not a profile\n")
        cls.server = make_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.root.cleanup()

    def setUp(self):
        engines = {
            "radiance-r9700": self.Stub(
                "radiance-r9700",
                "Radiance \u00b7 R9700",
                "http://127.0.0.1:8000",
                model={"id": "rad-7b"},
                caps={"chat": True},
            ),
            "llamacpp-4060ti": self.Stub("llamacpp-4060ti", "", "http://127.0.0.1:8001"),
        }
        self.engines_patch = mock.patch.dict(wsd.routes.ENGINES, engines, clear=True)
        self.profiles_patch = mock.patch.dict(
            wsd.routes.ENGINE_PROFILES, {"radiance-r9700": (str(self.dir), self.ACTIVE)}, clear=True
        )
        self.engines_patch.start()
        self.profiles_patch.start()
        self.addCleanup(mock.patch.stopall)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def error_for(self, path):
        status, payload = self.get(path)
        self.assertEqual(status, 404, path)
        self.assertIn("error", payload)
        return payload["error"]

    def test_engine_rows_describe_the_same_adapters_as_inference(self):
        status, payload = self.get("/api/engines")
        self.assertEqual(status, 200)
        first, second = payload["engines"]
        self.assertEqual(
            first,
            {
                "id": "radiance-r9700",
                "type": "radiance",
                "label": "Radiance \u00b7 R9700",
                "base_url": "http://127.0.0.1:8000",
                "status": "down",
                "model": "rad-7b",
                "capabilities": {"chat": True},
                "has_profiles": True,
            },
        )
        self.assertEqual(second["label"], "llamacpp-4060ti")  # no label, id stands in
        self.assertFalse(second["has_profiles"])
        self.assertIsNone(second["model"])

    def test_profiles_are_listed_active_first(self):
        status, payload = self.get("/api/engines/radiance-r9700/profiles")
        self.assertEqual(status, 200)
        self.assertEqual(payload["engine"], "radiance-r9700")
        self.assertEqual(payload["profiles_dir"], str(self.dir))
        self.assertEqual(payload["active_profile"], self.ACTIVE)
        self.assertEqual([row["name"] for row in payload["profiles"]], [self.ACTIVE, self.OTHER])
        self.assertEqual([row["active"] for row in payload["profiles"]], [True, False])
        self.assertEqual(payload["profiles"][0]["diff_vs_active"], "")
        self.assertEqual(
            payload["profiles"][1]["diff_vs_active"].splitlines()[:2], [f"--- {self.ACTIVE}", f"+++ {self.OTHER}"]
        )

    def test_a_profile_file_is_returned_as_text(self):
        status, payload = self.get(f"/api/engines/radiance-r9700/profiles/{self.OTHER}")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload, {"engine": "radiance-r9700", "name": self.OTHER, "text": "context: 4096\nretoken: auto\n"}
        )

    def test_nothing_is_written_or_renamed(self):
        before = sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.iterdir())
        self.get(f"/api/engines/radiance-r9700/profiles/{self.ACTIVE}")
        self.get("/api/engines/radiance-r9700/profiles")
        self.assertEqual(
            sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.iterdir()), before
        )

    def test_no_active_profile_means_no_active_row_and_no_diffs(self):
        with mock.patch.dict(wsd.routes.ENGINE_PROFILES, {"radiance-r9700": (str(self.dir), None)}):
            status, payload = self.get("/api/engines/radiance-r9700/profiles")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["active_profile"])
        self.assertEqual([row["active"] for row in payload["profiles"]], [False, False])
        self.assertEqual("".join(row["diff_vs_active"] for row in payload["profiles"]), "")

    def test_unknown_engine_is_a_404_with_a_reason(self):
        self.assertIn("no engine", self.error_for("/api/engines/apollo-x/profiles"))

    def test_engine_without_a_profiles_dir_is_a_404(self):
        self.assertIn("profiles_dir", self.error_for("/api/engines/llamacpp-4060ti/profiles"))

    def test_a_vanished_profile_directory_is_a_404_not_an_empty_list(self):
        with mock.patch.dict(wsd.routes.ENGINE_PROFILES, {"radiance-r9700": ("/nonexistent/profiles", None)}):
            self.assertIn("not a directory", self.error_for("/api/engines/radiance-r9700/profiles"))

    def test_unservable_profile_names_are_404(self):
        for suffix in (
            "%2e%2e%2fevil.yaml",  # never unquoted, so it is simply not a name
            "../../etc/passwd",
            "notes.txt",
            ".hidden.yaml",
            "absent.yaml",
            f"{self.ACTIVE}/extra.yaml",
        ):
            self.assertEqual(self.get(f"/api/engines/radiance-r9700/profiles/{suffix}")[0], 404, suffix)

    def test_unknown_subpaths_are_404_and_never_a_listing(self):
        for path in (
            "/api/engines/radiance-r9700/apply",
            "/api/engines/radiance-r9700/profiles/x/apply",
            "/api/engines/radiance-r9700",
            "/api/engines/",
        ):
            status, payload = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn("profiles", payload, path)

    def test_raising_prefix_handler_is_a_500_with_the_error_type(self):
        def boom(_tail):
            raise KeyError("gone")

        with mock.patch.dict(wsd.server.PREFIX_ROUTES, {"/api/boom/": boom}):
            try:
                with urllib.request.urlopen(self.base + "/api/boom/anything") as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "KeyError"})

    def test_profile_routes_refuse_writes(self):
        # the POST protection answers before any route lookup: these routes never write
        req = urllib.request.Request(self.base + "/api/engines/radiance-r9700/profiles", method="POST", data=b"{}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)


class NoMonkeypatchTest(unittest.TestCase):
    """The report route must be served by a handler, not by replacing server internals."""

    def test_dispatch_still_belongs_to_the_server(self):
        self.assertEqual(wsd.server.Handler._dispatch.__module__, "wsd.server")
        self.assertEqual(wsd.server.Handler._dispatch.__qualname__, "Handler._dispatch")

    def test_routes_module_never_touches_the_handler(self):
        src = (Path(__file__).resolve().parent.parent / "wsd" / "routes.py").read_text()
        for needle in ("_dispatch", "server.Handler", "Handler."):
            self.assertNotIn(needle, src)


class ReportRouteHttpTest(unittest.TestCase):
    """Every report route, both modes: download headers, or a JSON error."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        records = Path(cls.tmp.name)
        (records / "perf").mkdir()
        (records / "tasks").mkdir()
        (records / "reviews").mkdir()
        job = {"task": "T-001", "schema": 1, "rounds": 1, "first_time_accept": True,
               "worker_seconds": 600, "accepted_at": "2026-11-20T08:00:00+00:00"}
        (records / "perf" / "tasks.jsonl").write_text(json.dumps(job) + "\n", encoding="utf-8")
        (records / "tasks" / "T-001.md").write_text("# T-001 (M5): One job\n\nAllowed: nothing\n",
                                                    encoding="utf-8")
        (records / "reviews" / "T-001.md").write_text("ACCEPTED: fine.\n", encoding="utf-8")
        bench = [{"task": "T-001", "variant": "old", "engine": {"available": True,
                 "active_decode_tps": 10}},
                 {"task": "T-001", "variant": "new", "engine": {"available": True,
                  "active_decode_tps": 20}}]
        (records / "perf" / "bench.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in bench), encoding="utf-8")
        cls.original = wsd.routes.RECORDS_DIR
        wsd.routes.RECORDS_DIR = str(records)
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        wsd.routes.RECORDS_DIR = cls.original

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, dict(resp.headers), body

    def download(self, path, filename=None):
        status, headers, body = self.get(path)
        self.assertEqual(status, 200, body[:200])
        self.assertEqual(headers["Content-Type"], "text/markdown; charset=utf-8")
        if filename is not None:
            self.assertEqual(headers["Content-Disposition"], f'attachment; filename="{filename}"')
        self.assertEqual(headers["Cache-Control"], "no-store")
        return body.decode("utf-8")

    def json_error(self, path, status):
        got, headers, body = self.get(path)
        self.assertEqual(got, status, body[:200])
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Content-Disposition", headers)
        return json.loads(body)["error"]

    def test_every_route_and_mode_names_its_file(self):
        for path, filename in [
            ("/api/jobs/T-001/report.md", "panoptes-T-001-report.md"),
            ("/api/jobs/T-001/report-private.md", "panoptes-T-001-report-private.md"),
            ("/api/reports/jobs/shareable/T-001.md", "panoptes-jobs-shareable.md"),
            ("/api/reports/jobs/private/T-001.md", "panoptes-jobs-private.md"),
            ("/api/reports/lab/shareable/old/new.md", "panoptes-lab-shareable.md"),
            ("/api/reports/lab/private/old/new.md", "panoptes-lab-private.md"),
        ]:
            with self.subTest(path=path):
                text = self.download(path, filename)
                self.assertIn("- report_schema_version: 2", text)
                self.assertIn("Redactions: 0", text)
                self.assertTrue(text.endswith("\n"))

    def test_the_mode_reaches_the_download(self):
        self.assertIn("- mode: Shareable (`shareable`)",
                      self.download("/api/reports/jobs/shareable/T-001.md"))
        self.assertIn("- mode: Private (`private`)",
                      self.download("/api/reports/jobs/private/T-001.md"))

    def test_an_unknown_job_or_variant_is_a_json_error(self):
        for path in ("/api/reports/jobs/shareable/T-999.md",
                     "/api/reports/jobs/shareable/T-001,T-999.md",
                     "/api/reports/jobs/shareable/.md",
                     "/api/reports/lab/shareable/old/absent.md"):
            with self.subTest(path=path):
                self.assertEqual(self.json_error(path, 404)[:2], "no")

    def test_a_mode_that_does_not_exist_is_a_json_400(self):
        for path in ("/api/jobs/T-001/report-public.md",
                     "/api/reports/jobs/public/T-001.md",
                     "/api/reports/lab/public/old/new.md"):
            with self.subTest(path=path):
                error = self.json_error(path, 400)
                self.assertIn("unknown report mode", error)
                self.assertIn("shareable, private", error)

    def test_more_job_ids_than_the_cap_is_a_json_400(self):
        ids = ",".join(f"T-{n:04d}" for n in range(201))
        error = self.json_error(f"/api/reports/jobs/shareable/{ids}.md", 400)
        self.assertEqual(error, "too many job ids: 201, the cap is 200")
        # the cap itself is fine to ask for, even if most ids are unknown
        self.assertEqual(self.json_error(
            f"/api/reports/jobs/shareable/{','.join(f'T-{n:04d}' for n in range(200))}.md",
            404)[:3], "no ")

    def test_existing_routes_are_untouched(self):
        status, headers, body = self.get("/api/jobs")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/json"))
        self.assertIn("jobs", json.loads(body))
        status, headers, _body = self.get("/api/lab/variants")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/json"))
        status, headers, _body = self.get("/api/lab/compare/old/new")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/json"))
        status, headers, _body = self.get("/api/jobs/T-001/extra")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
