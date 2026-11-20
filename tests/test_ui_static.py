import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from wsd.server import make_server


class UiStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()

    def check_assets(self, page_path):
        """Every href= and src= in the page served at page_path must return 200."""
        status, _, body = self.get(page_path)
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        refs = re.findall(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", html)
        self.assertTrue(refs, f"no href/src references found in {page_path}")
        for ref in refs:
            url = urllib.parse.urljoin(page_path, ref)
            try:
                status, _, _ = self.get(url)
            except urllib.error.HTTPError as exc:
                self.fail(f"{page_path}: {ref!r} -> {url} returned {exc.code}")
            self.assertEqual(status, 200, f"{page_path}: {ref!r} -> {url}")

    def test_theme_css_served(self):
        status, ctype, body = self.get("/static/ui/theme.css")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/css"), ctype)
        self.assertIn(b"--ok", body)

    def test_geom_js_served(self):
        status, ctype, body = self.get("/static/ui/geom.js")
        self.assertEqual(status, 200)
        self.assertTrue("javascript" in ctype or "ecmascript" in ctype, ctype)
        self.assertIn(b"export", body)

    def test_kit_js_served(self):
        status, ctype, body = self.get("/static/ui/kit.js")
        self.assertEqual(status, 200)
        self.assertTrue("javascript" in ctype or "ecmascript" in ctype, ctype)
        self.assertIn(b"export function dial", body)

    def test_demo_html_served(self):
        status, ctype, body = self.get("/static/demo.html")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"), ctype)
        self.assertIn(b"./ui/kit.js", body)
        self.assertIn(b"./ui/theme.css", body)

    def test_app_js_served(self):
        status, ctype, body = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertTrue("javascript" in ctype or "ecmascript" in ctype, ctype)
        self.assertIn(b'from "./ui/kit.js"', body)

    def test_inference_js_served(self):
        status, ctype, body = self.get("/static/inference.js")
        self.assertEqual(status, 200)
        self.assertTrue("javascript" in ctype or "ecmascript" in ctype, ctype)
        self.assertIn(b'from "./ui/kit.js"', body)

    def test_inference_has_throughput_levels_card(self):
        _, _, body = self.get("/static/inference.js")
        for marker in (
            b"Throughput, three ways",
            b"throughput_levels",
            b"Active generation",
            b"Effective request",
            b"Queue share",
            b"new-token prefill",
            b"milestone 4",
        ):
            self.assertIn(marker, body)
        # T-013's live measured dials must stay in place.
        self.assertIn(b"Generation tok/s", body)
        self.assertIn(b"Prefill tok/s", body)

    def test_index_served_at_root(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"), ctype)
        self.assertIn(b"PANOPTES", body)

    def test_index_assets_resolve(self):
        self.check_assets("/")

    def test_index_has_six_tabs(self):
        _, _, body = self.get("/")
        html = body.decode("utf-8")
        tabs = re.findall(r"""<button[^>]*role="tab"[^>]*aria-selected="(true|false)"[^>]*>""", html)
        self.assertEqual(len(tabs), 6)
        self.assertEqual(tabs.count("true"), 1)
        for name in ("hardware", "inference", "engines", "agents", "lab", "control"):
            self.assertIn(f'data-tab="{name}"', html)

    def test_demo_assets_resolve(self):
        self.check_assets("/static/demo.html")

    def test_inference_has_multi_engine_markers(self):
        _, _, body = self.get("/static/inference.js")
        for marker in (
            b"eng-pills",
            b"eng-pill",
            b"#inference/",
            b"Not exposed by",
            b"No inference engines configured",
            b"config/panoptes.toml",
            b"capabilities",
            b"latency_histograms",
            b"throughput_levels",
            b"per_request",
            b"speculative",
            b"transfers",
            b"experts",
        ):
            self.assertIn(marker, body)

    def test_theme_has_engine_pills(self):
        _, _, body = self.get("/static/ui/theme.css")
        self.assertIn(b".eng-pills", body)
        self.assertIn(b".eng-pill", body)

    def test_control_js_served_with_markers(self):
        status, ctype, body = self.get("/static/control.js")
        self.assertEqual(status, 200)
        self.assertTrue("javascript" in ctype or "ecmascript" in ctype, ctype)
        for marker in (
            b'from "./ui/kit.js"',
            b"lamp(",
            b"progressRing(",
            b"/api/control/status",
            b"/api/control/logs",
            b"/api/control/${op}",
            b"X-WSD-Token",
            b"wsd-token",
            b"Content-Type",
            b"confirm",
            b"/api/inference",
            b"radiance-r9700",
            b"A supervised worker task is running: Stop and Restart are locked",
            b"Restart Radiance? The model will reload (about 30 s); requests in progress are refused by the helper.",
            b"button#ctl-",
            b"no operation is waiting for confirmation",
        ):
            self.assertIn(marker, body)

    def test_index_has_control_tab_markup(self):
        _, _, body = self.get("/")
        for marker in (
            b'id="ctl-start"',
            b'id="ctl-stop"',
            b'id="ctl-restart"',
            b'id="ctl-confirm-yes"',
            b'id="ctl-result"',
            b'id="ctl-logs"',
            b'id="ctl-dryrun-badge"',
            b'class="logbox"',
            b"/static/control.js",
        ):
            self.assertIn(marker, body)

    def test_theme_has_control_styles(self):
        _, _, body = self.get("/static/ui/theme.css")
        self.assertIn(b".logbox", body)
        self.assertIn(b".warn-note", body)
        self.assertIn(b".confirm-step", body)
        self.assertIn(b"pre-wrap", body)

    def test_index_element_ids_are_unique(self):
        """A duplicated id silently steals a lookup: getElementById answers with
        the first match in document order, not the element the script meant."""
        _status, _ctype, body = self.get("/")
        ids = re.findall(rb'id="([^"]+)"', body)
        self.assertEqual({i for i in ids if ids.count(i) > 1}, set())
        self.assertIn(b'id="ctl-restart"', body)  # the Restart button
        self.assertIn(b'id="ctl-restart-policy"', body)  # the restart-policy <dd>


    def test_engines_js_served(self):
        status, ctype, body = self.get("/static/engines.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        self.assertIn(b'from "./ui/kit.js"', body)

    def test_engines_has_readonly_markers(self):
        _, _, body = self.get("/static/engines.js")
        for marker in [
            b"/api/engines",
            b"/profiles",
            b"Diff vs active",
            b"Full file",
            b"identical to active",
            b"Switching restarts Radiance. Every switch is previewed, backed up and journalled; a failed start restores the previous profile.",
        ]:
            self.assertIn(marker, body)

    def test_engines_profiles_note_is_conditional(self):
        """B-004: the read-only note shows only on cards that list profiles."""
        _, _, body = self.get("/static/engines.js")
        for marker in (
            b"roNote.hidden = true;",
            b"roNote.hidden = st.profilesFailed || !st.profiles || st.profiles.length === 0;",
        ):
            self.assertIn(marker, body)

    def test_engines_posts_only_with_confirm(self):
        """T-041: a switch must never fire without the in-page confirm — every
        POST goes through the single helper that sends {"confirm": true}."""
        _, _, body = self.get("/static/engines.js")
        js = body.decode()
        self.assertEqual(js.count('method: "POST"'), 1)
        self.assertRegex(
            js,
            r'method:\s*"POST",\s*headers:\s*\{[^}]*X-WSD-Token[^}]*\},'
            r"\s*body:\s*JSON\.stringify\(\{\s*confirm:\s*true\s*\}\)",
        )
        self.assertIn("X-WSD-Token", js)

    def test_index_has_engines_tab_markup(self):
        _, _, body = self.get("/")
        html = body.decode()
        self.assertIn('id="page-engines"', html)
        self.assertIn('<script type="module" src="/static/engines.js"></script>', html)

    def test_theme_has_engines_styles(self):
        _, _, body = self.get("/static/ui/theme.css")
        self.assertIn(b".eng-table", body)
        self.assertIn(b".diff .add", body)
        self.assertIn(b".chip.on", body)

    def test_agents_js_served(self):
        status, ctype, body = self.get("/static/agents.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)

    def test_agents_js_markers(self):
        _, _, body = self.get("/static/agents.js")
        for marker in [
            b"/api/jobs",
            b"setInterval(tick, 30000)",
            b"Accepted tasks per worker-hour",
            b"First-time acceptance",
            b"Energy per accepted task",
            b"Effective worker speed",
            b"measured jobs",
            b"first time",
            b"#agents?",
            b"replaceState",
            b"calculated",
            b"partial",
            b"unavailable",
            b"engine-lifetime",
            b"ag-detail-engine-h",
            b"Export .md",
            b"${suffix}.md",
            b"/api/reports/jobs/",
            b"ag-export-mode",
            b"encodeURIComponent",
            b'setAttribute("download"',
            b"panoptes-",
        ]:
            self.assertIn(marker, body)

    def test_index_has_agents_tab_markup(self):
        _, _, body = self.get("/")
        html = body.decode()
        self.assertIn('id="page-agents"', html)
        self.assertIn('<script type="module" src="/static/agents.js"></script>', html)
        self.assertIn('id="ag-f-milestone"', html)
        self.assertIn('id="ag-f-outcome"', html)
        self.assertIn('id="ag-f-first"', html)
        self.assertIn("Engine lifetime at review time (not per job)", html)

    def test_theme_has_agents_styles(self):
        _, _, body = self.get("/static/ui/theme.css")
        for marker in [b".ag-stats", b".ag-stat", b".ag-table", b".ag-partial",
                       b".ag-calc", b".ag-detail", b".ag-review", b".ag-export"]:
            self.assertIn(marker, body)

    def test_report_exports_are_plain_links_with_a_mode_choice(self):
        """No fetch and no Blob: each export is an <a download> whose href the module
        builds from what is on screen, and both report modes are offered on each tab."""
        _, _, page = self.get("/")
        html = page.decode()
        for element in ('id="ag-export-mode"', 'id="ag-export-set"',
                        'id="lab-export-mode"', 'id="lab-export"'):
            self.assertIn(element, html)
        # a link with no href is not clickable, so the disabled state is real markup
        self.assertIn('id="lab-export" class="ag-export-btn is-disabled" aria-disabled="true"', html)
        for name in ("agents.js", "lab.js"):
            with self.subTest(name=name):
                _, _, body = self.get(f"/static/{name}")
                self.assertIn(b"encodeURIComponent", body)
                for gone in (b"new Blob", b"createObjectURL", b"navigator.clipboard"):
                    self.assertNotIn(gone, body)
        _, _, agents = self.get("/static/agents.js")
        self.assertIn(b"/api/reports/jobs/", agents)
        self.assertIn(b"/api/jobs/", agents)
        _, _, lab = self.get("/static/lab.js")
        self.assertIn(b"/api/reports/lab/", lab)
        self.assertIn(b"is-disabled", lab)
        _, _, css = self.get("/static/ui/theme.css")
        self.assertIn(b".ag-export-mode", css)
        self.assertIn(b".ag-export-btn.is-disabled", css)

    def test_app_js_is_the_router_for_the_tab_modules(self):
        """app.js publishes each applied route; tab modules subscribe to it."""
        _, _, body = self.get("/static/app.js")
        self.assertIn(b"panoptes:route", body)
        self.assertIn(b"window.panoptesRoute", body)
        self.assertIn(b"new CustomEvent", body)

    def test_tab_modules_listen_instead_of_fighting_the_router(self):
        """No hash re-assertion: no hashchange listener, no timing tricks."""
        for name in ("inference.js", "agents.js", "lab.js", "lab-trends.js"):
            with self.subTest(name=name):
                _, _, body = self.get(f"/static/{name}")
                self.assertIn(b"panoptes:route", body)
                for gone in (b"hashchange", b"oldURL", b"armEnforce", b"getEntriesByType"):
                    self.assertNotIn(gone, body)

    def test_agents_js_has_live_workers_card(self):
        _, _, body = self.get("/static/agents.js")
        for marker in [
            b"Live workers",
            b'from "./ui/kit.js"',
            b"lamp(",
            b"segBar(",
            b"/api/harnesses",
            b"/api/inference",
            b"LIVE_POLL_MS = 5000",
            b"setInterval(tickLive, LIVE_POLL_MS)",
            b"setInterval(tickClock, 1000)",
            b"65536",
            b"max_model_len",
            b"#agents?task=",
            b"elapsed",
            b"last activity",
            b"compactions",
            b"peak",
            b"reasoning",
            b"turns",
            b"tools:",
            b'"Idle"',
            b"capabilities",
        ]:
            self.assertIn(marker, body)

    def test_theme_has_live_worker_styles(self):
        _, _, body = self.get("/static/ui/theme.css")
        for marker in [b".ag-live-row", b".ag-live-head", b".ag-live-task",
                       b".ag-live-mid", b".ag-live-bar", b".ag-live-tools",
                       b".ag-live-act", b".ag-live-idle", b".ag-live-warns",
                       b".ag-warn"]:
            self.assertIn(marker, body)

    def test_lab_js_served(self):
        status, ctype, body = self.get("/static/lab.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        for marker in [
            b'from "./lab-format.js"',
            b"/api/lab/variants",
            b"/api/lab/compare/",
            b"panoptes:route",
            b"replaceState",
            b"setInterval(tick, 30000)",
            b"No benchmark runs recorded yet.",
            b"a second variant is needed",
            b"NOISE_NOTE",
        ]:
            self.assertIn(marker, body)

    def test_lab_format_js_served(self):
        status, ctype, body = self.get("/static/lab-format.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        for marker in [
            b"export function fmtSide",
            b"export function fmtDiff",
            b"export function diffClass",
            b"export function parseLabQuery",
            b"export function buildLabHash",
            b'"no data"',
            b"not distinguishable from run-to-run variation",
            b"lab-ok",
            b"lab-hot",
        ]:
            self.assertIn(marker, body)

    def test_index_has_lab_tab_markup(self):
        _, _, body = self.get("/")
        html = body.decode()
        self.assertIn('id="page-lab"', html)
        self.assertIn('<script type="module" src="/static/lab.js"></script>', html)
        self.assertIn('id="lab-baseline"', html)
        self.assertIn('id="lab-modified"', html)
        self.assertIn("Per benchmark run (job-scoped), not engine-lifetime.", html)
        self.assertIn("Pick two different variants to compare.", html)

    def test_theme_has_lab_styles(self):
        _, _, body = self.get("/static/ui/theme.css")
        for marker in [b".lab-ok { color: var(--ok); }", b".lab-hot { color: var(--hot); }",
                       b".lab-noise"]:
            self.assertIn(marker, body)

    def test_lab_trends_js_served(self):
        status, ctype, body = self.get("/static/lab-trends.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        for marker in [
            b'from "./lab-trends-format.js"',
            b'from "./ui/geom.js"',
            b"/api/lab/trends",
            b"panoptes:route",
            b"createElementNS",
            b'tabindex: 0',
            b"aria-label",
            b"lt-hollow",
            b"lt-back",
            b"lt-median",
            b"lt-marker",
            b'getJSON("/api/lab/trends")',
            b"no ${measure.unit} recorded",
            b"markerLabels(group, MARK_ROWS)",
            b"layoutMarkerLabels(raw, BOX.w - BOX.r, MARK_CHAR_W, MARK_ROWS)",
            b'label.anchor === "end" ? label.right : label.left',
            b'MARK_TOP + label.row * MARK_ROW_H + 7',
            b'el("details", "warn-note", warnsEl)',
            b'class: "lt-marker-label"',
            b"Nothing to plot yet",
        ]:
            self.assertIn(marker, body)
        # the only http:// in the file is the SVG namespace, never a fetch
        for gone in (b"console.", b"getContext", b"hashchange", b"oldURL",
                     b"armEnforce", b"getEntriesByType", b"fetch(\"http"):
            self.assertNotIn(gone, body)

    def test_lab_trends_format_js_served(self):
        status, ctype, body = self.get("/static/lab-trends-format.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        for marker in [
            b"export function parseStamp",
            b"export function median",
            b"export function rollingMedian",
            b"export function markerSlot",
            b"export function markerLabels",
            b"export function scaleY",
            b"export function summary",
            b"export function pointValue",
            b"export function pointComplete",
            b"export function layoutMarkerLabels",
            b"export function groupWarnings",
            b'["measured", "calculated"]',
        ]:
            self.assertIn(marker, body)

    def test_index_has_lab_trends_card(self):
        _, _, body = self.get("/")
        html = body.decode()
        self.assertIn('id="lab-trends-card"', html)
        self.assertIn('id="lab-trends-charts"', html)
        self.assertIn('id="lab-trends-summary"', html)
        self.assertIn('id="lab-trends-warnings"', html)
        self.assertIn('<script type="module" src="/static/lab-trends.js"></script>', html)
        self.assertIn("never drawn at zero", html)
        self.assertIn('id="lab-trends-empty" hidden', html)

    def test_index_loads_nothing_off_this_machine(self):
        """The page fetches nothing outside 127.0.0.1: no CDN, no font host."""
        _, _, body = self.get("/")
        html = body.decode()
        refs = re.findall(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", html)
        self.assertTrue(refs)
        for ref in refs:
            self.assertNotRegex(ref, r"^(?:https?:)?//", f"external reference: {ref}")
            self.assertTrue(ref.startswith("/static/"), f"not a local asset: {ref}")

    def test_theme_has_lab_trends_styles(self):
        _, _, body = self.get("/static/ui/theme.css")
        for marker in [b"#lab-trends-charts", b".lt-cell", b".lt-chart", b".lt-pt",
                       b".lt-pt.lt-hollow { fill: none; }", b".lt-pt.lt-back",
                       b".lt-pt:focus", b".lt-median", b".lt-marker", b".lt-skip",
                       b".lt-marker-label", b".lt-warn-list",
                       b"var(--violet)", b"var(--warn)", b"var(--info)", b"var(--muted)"]:
            self.assertIn(marker, body)

    def test_guardian_chip_is_the_header_lamp_before_the_api_lamp(self):
        """The status chip is a lamp in the header, to the left of the API lamp."""
        _, _, body = self.get("/")
        html = body.decode()
        header = html[html.index("<header"):html.index("</header>")]
        self.assertLess(header.index('id="guardian-chip"'), header.index('id="api-lamp"'))
        start = header.index('<div id="guardian-chip"')
        chip = header[start:header.index(">", start) + 1]
        self.assertIn('role="status"', chip)  # read out again whenever it changes
        self.assertNotIn("<button", chip)

    def test_guardian_chip_is_not_a_control(self):
        """M6 is read-only: the chip is a div, never a button or a link."""
        _, _, body = self.get("/")
        html = body.decode()
        self.assertNotRegex(html, r"<(?:button|a)\b[^>]*guardian-chip")
        _, _, script = self.get("/static/guardian-chip.js")
        self.assertNotIn("POST", script.decode())  # it only ever reads

    def test_guardian_chip_js_is_served(self):
        for path in ("/static/guardian-chip.js", "/static/guardian-chip-format.js"):
            status, ctype, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("javascript", ctype, path)
        _, _, script = self.get("/static/guardian-chip.js")
        source = script.decode()
        for marker in ['fetch("/api/guardian/state"', 'document.getElementById("guardian-chip")',
                       "setInterval(tick, POLL_MS)", "document.hidden", "visibilitychange",
                       "NO_DATA", 'from "./ui/kit.js"', 'from "./guardian-chip-format.js"']:
            self.assertIn(marker, source)
        _, _, fmt = self.get("/static/guardian-chip-format.js")
        for marker in [b"export function chipLabel", b"export function chipClass",
                       b"export const NO_DATA", '"Guardian · no data"'.encode(),
                       b"export const UNKNOWN_MARK", b"export const LAMP_STATE"]:
            self.assertIn(marker, fmt)

    def test_index_loads_the_guardian_chip_module(self):
        _, _, body = self.get("/")
        self.assertIn('<script type="module" src="/static/guardian-chip.js"></script>',
                      body.decode())

    def test_theme_has_guardian_chip_styles(self):
        _, _, body = self.get("/static/ui/theme.css")
        for marker in [b".gchip", b".gchip-ok", b".gchip-warn", b".gchip-hot",
                       b".gchip-off", b".gchip-unknown", b"var(--hot)", b"var(--muted)"]:
            self.assertIn(marker, body)


if __name__ == "__main__":
    unittest.main()
