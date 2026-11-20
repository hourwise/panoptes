"""T-039: Radiance control schema, strict profile parser, validate/preview,
read-only /controls and /profiles/<name>/preview routes.

Nothing here may read ``~/Applications/Radiance``: the real profile shape
lives on as ``tests/fixtures/controls/flashnext.yaml``.
"""

import copy
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import wsd.routes
from wsd import controls
from wsd.server import make_server

FIXTURES = Path(__file__).parent / "fixtures" / "controls"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def patched(active: dict, **overrides) -> dict:
    """Deep copy of ``active`` with dotted-path leaves replaced."""
    tree = copy.deepcopy(active)
    for path, value in overrides.items():
        node = tree
        parts = path.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return tree


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.active = controls.parse_profile(fixture_text("flashnext.yaml"))

    def test_the_real_profile_shape_parses_to_typed_values(self):
        tree = self.active
        self.assertEqual(sorted(tree), ["memory", "models", "server"])
        self.assertEqual(tree["server"], {"host": "0.0.0.0", "port": 8000, "live": True, "log": "info"})
        self.assertIsInstance(tree["server"]["port"], int)
        self.assertIs(tree["server"]["live"], True)
        model = tree["models"]["flashnext"]
        self.assertEqual(model["model"], "/models/qwen3.8-next-flash-iqr-v4.rad")
        self.assertEqual(model["num-speculative-tokens"], 5)
        self.assertIs(model["weights-disk-tier"], False)
        self.assertIsInstance(model["max-model-len"], int)
        self.assertEqual(
            sorted(tree["memory"]), ["gpu-headroom-mib", "host-headroom-mib"]
        )

    def test_spec6_differs_in_exactly_one_leaf(self):
        spec6 = controls.parse_profile(fixture_text("flashnext-spec6.yaml"))
        flat_active = controls.flatten(self.active)
        flat_spec6 = controls.flatten(spec6)
        self.assertEqual(
            [p for p in flat_active if flat_active[p] != flat_spec6.get(p)],
            ["models.flashnext.num-speculative-tokens"],
        )

    def test_comments_blank_lines_and_quoted_strings_are_accepted(self):
        tree = controls.parse_profile(
            "# a comment\n\nserver:\n  host: \"0.0.0.0\"  \n\n  # another\n  port: 8000\n"
        )
        self.assertEqual(tree, {"server": {"host": "0.0.0.0", "port": 8000}})

    def test_negative_numbers_are_integers(self):
        self.assertEqual(controls.parse_profile("offset: -5\n"), {"offset": -5})

    ODDITIES = {
        "bad_list.yaml": 5,
        "bad_anchor.yaml": 3,
        "bad_tab.yaml": 3,
        "bad_flow.yaml": 4,
        "bad_quote.yaml": 3,
        "bad_dup.yaml": 5,
        "bad_indent.yaml": 3,
        "bad_skip.yaml": 5,
        "bad_comment.yaml": 3,
    }

    def test_oddities_are_rejected_with_the_line_number(self):
        for name, line in self.ODDITIES.items():
            with self.subTest(fixture=name):
                with self.assertRaises(controls.ProfileParseError) as caught:
                    controls.parse_profile(fixture_text(name))
                self.assertEqual(str(caught.exception).split(":")[0], f"line {line}")

    def test_invalid_key_is_rejected(self):
        with self.assertRaisesRegex(controls.ProfileParseError, r"^line 2:"):
            controls.parse_profile("server:\n  bad key: 1\n")

    def test_line_without_a_colon_is_rejected(self):
        with self.assertRaisesRegex(controls.ProfileParseError, r"^line 1:"):
            controls.parse_profile("server\n")

    def test_empty_text_is_an_empty_profile(self):
        self.assertEqual(controls.parse_profile("# only a comment\n"), {})


class ValidateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.active = controls.parse_profile(fixture_text("flashnext.yaml"))
        cls.schema = controls.build_schema(cls.active)

    def errors_for(self, candidate):
        return controls.validate(self.schema, self.active, candidate)["errors"]

    def test_the_active_profile_validates_against_itself(self):
        verdict = controls.validate(self.schema, self.active, copy.deepcopy(self.active))
        self.assertEqual(verdict, {"valid": True, "errors": [], "warnings": []})

    def test_a_leaf_outside_the_schema_is_rejected(self):
        errors = self.errors_for(patched(self.active, **{"server.tls": True}))
        self.assertEqual(errors, ["server.tls: not a declared control"])

    def test_a_renamed_model_key_is_rejected_as_unknown(self):
        candidate = copy.deepcopy(self.active)
        candidate["models"]["flashnx01"] = candidate["models"].pop("flashnext")
        errors = self.errors_for(candidate)
        self.assertTrue(any("models.flashnx01" in e for e in errors), errors)
        self.assertFalse(any(e.startswith("models.flashnext.") and "unknown" in e for e in errors))

    def test_out_of_range_is_rejected(self):
        errors = self.errors_for(patched(self.active, **{"models.flashnext.num-speculative-tokens": 9}))
        self.assertEqual(len(errors), 1)
        self.assertIn("models.flashnext.num-speculative-tokens", errors[0])
        self.assertIn("0..8", errors[0])

    def test_wrong_types_are_rejected(self):
        errors = self.errors_for(patched(self.active, **{"models.flashnext.weights-disk-tier": "false"}))
        self.assertIn("expected true or false", errors[0])
        errors = self.errors_for(patched(self.active, **{"models.flashnext.max-model-len": "65536"}))
        self.assertIn("expected an integer", errors[0])
        # YAML-bool is not an int
        errors = self.errors_for(patched(self.active, **{"models.flashnext.num-speculative-tokens": True}))
        self.assertIn("expected an integer", errors[0])

    def test_an_enum_value_outside_the_allowed_set_is_rejected(self):
        errors = self.errors_for(patched(self.active, **{"models.flashnext.placement": "plain"}))
        self.assertIn("'plain' is not one of 'expert_tiered'", errors[0])

    def test_server_and_model_identity_are_fixed(self):
        for path, value in (
            ("server.host", "127.0.0.1"),
            ("server.port", 8001),
            ("models.flashnext.model", "/models/other.rad"),
            ("models.flashnext.tp", 2),
        ):
            with self.subTest(path=path):
                errors = self.errors_for(patched(self.active, **{path: value}))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(path, errors[0])
                self.assertIn("fixed", errors[0])

    def test_a_leaf_the_active_profile_sets_may_not_be_dropped(self):
        candidate = copy.deepcopy(self.active)
        del candidate["memory"]["gpu-headroom-mib"]
        errors = self.errors_for(candidate)
        self.assertEqual(len(errors), 1)
        self.assertIn("memory.gpu-headroom-mib", errors[0])
        self.assertIn("missing", errors[0])

    def test_expert_pool_plus_host_headroom_must_fit_the_machine(self):
        candidate = patched(
            self.active,
            **{"models.flashnext.host-pool-mib": 98304, "memory.host-headroom-mib": 32768},
        )
        errors = self.errors_for(candidate)
        self.assertEqual(len(errors), 1)
        self.assertIn("models.flashnext.host-pool-mib + memory.host-headroom-mib = 131072", errors[0])
        self.assertIn("110592", errors[0])

    def test_the_cross_check_boundary_is_inclusive(self):
        candidate = patched(
            self.active,
            **{"models.flashnext.host-pool-mib": 98304, "memory.host-headroom-mib": 12288},
        )
        self.assertTrue(controls.validate(self.schema, self.active, candidate)["valid"])

    def test_a_bad_context_length_warns_but_does_not_block(self):
        candidate = patched(self.active, **{"models.flashnext.max-model-len": 65000})
        verdict = controls.validate(self.schema, self.active, candidate)
        self.assertTrue(verdict["valid"])
        self.assertEqual(verdict["errors"], [])
        self.assertEqual(len(verdict["warnings"]), 1)
        self.assertIn("multiple of 1024", verdict["warnings"][0])


class PreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.active = controls.parse_profile(fixture_text("flashnext.yaml"))
        cls.spec6 = controls.parse_profile(fixture_text("flashnext-spec6.yaml"))
        cls.schema = controls.build_schema(cls.active)

    def test_spec6_previews_as_exactly_one_change(self):
        changes = controls.preview(self.schema, self.active, self.spec6)
        self.assertEqual(
            changes,
            [
                {
                    "path": "models.flashnext.num-speculative-tokens",
                    "from": 5,
                    "to": 6,
                    "unit": "tokens",
                    "restart_required": True,
                    "quality_sensitive": False,
                }
            ],
        )

    def test_an_identical_profile_changes_nothing(self):
        self.assertEqual(controls.preview(self.schema, self.active, copy.deepcopy(self.active)), [])

    def test_quality_sensitivity_and_units_come_from_the_schema(self):
        candidate = patched(
            self.active,
            **{"models.flashnext.reasoning-effort": "high", "models.flashnext.host-pool-mib": 49152},
        )
        changes = {c["path"]: c for c in controls.preview(self.schema, self.active, candidate)}
        self.assertTrue(changes["models.flashnext.reasoning-effort"]["quality_sensitive"])
        self.assertEqual(changes["models.flashnext.host-pool-mib"]["unit"], "MiB")
        self.assertTrue(all(c["restart_required"] for c in changes.values()))

    def test_a_dropped_leaf_previews_as_to_none(self):
        candidate = copy.deepcopy(self.active)
        del candidate["memory"]["gpu-headroom-mib"]
        changes = controls.preview(self.schema, self.active, candidate)
        self.assertEqual([c["to"] for c in changes], [None])


class SchemaTest(unittest.TestCase):
    def test_model_paths_use_the_active_profile_model_key(self):
        active = controls.parse_profile(fixture_text("flashnext.yaml"))
        names = {c.name for c in controls.build_schema(active).controls}
        self.assertIn("models.flashnext.num-speculative-tokens", names)
        self.assertNotIn("models.<name>.num-speculative-tokens", names)

    def test_without_an_active_profile_the_model_key_is_a_placeholder(self):
        names = {c.name for c in controls.build_schema(None).controls}
        self.assertIn("models.<name>.model", names)


class EngineControlRoutesTest(unittest.TestCase):
    """GET /api/engines/<id>/controls and .../profiles/<name>/preview."""

    ACTIVE = "flashnext.yaml"
    CANDIDATE = "flashnext-spec6.yaml"
    BROKEN = "bad-dup.yaml"

    class Stub:
        def __init__(self, engine_id):
            self.engine_id = engine_id

        def snapshot(self):
            return {"id": self.engine_id, "type": "stub"}

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.root.name)
        shutil.copy(FIXTURES / cls.ACTIVE, cls.dir / cls.ACTIVE)
        shutil.copy(FIXTURES / "flashnext-spec6.yaml", cls.dir / cls.CANDIDATE)
        shutil.copy(FIXTURES / "bad_dup.yaml", cls.dir / cls.BROKEN)
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
            "radiance-r9700": self.Stub("radiance-r9700"),
            "vllm-r9700": self.Stub("vllm-r9700"),
            "llamacpp-4060ti": self.Stub("llamacpp-4060ti"),
        }
        self.engines_patch = mock.patch.dict(wsd.routes.ENGINES, engines, clear=True)
        self.profiles_patch = mock.patch.dict(
            wsd.routes.ENGINE_PROFILES,
            {
                "radiance-r9700": (str(self.dir), self.ACTIVE),
                "vllm-r9700": (str(self.dir), self.ACTIVE),  # profiles_dir, but no control schema
            },
            clear=True,
        )
        self.types_patch = mock.patch.dict(
            wsd.routes.ENGINE_TYPES,
            {"radiance-r9700": "radiance", "vllm-r9700": "vllm", "llamacpp-4060ti": "llama.cpp"},
            clear=True,
        )
        for patcher in (self.engines_patch, self.profiles_patch, self.types_patch):
            patcher.start()
        self.addCleanup(mock.patch.stopall)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # ---- /controls --------------------------------------------------------

    def test_controls_lists_the_approved_schema_for_the_active_profile(self):
        status, payload = self.get("/api/engines/radiance-r9700/controls")
        self.assertEqual(status, 200)
        self.assertEqual(payload["engine"], "radiance-r9700")
        by_name = {c["name"]: c for c in payload["controls"]}
        self.assertIn("server.host", by_name)
        self.assertEqual(by_name["server.host"]["type"], "fixed")
        self.assertEqual(
            by_name["models.flashnext.num-speculative-tokens"],
            {
                "name": "models.flashnext.num-speculative-tokens",
                "type": "int",
                "min": 0,
                "max": 8,
                "values": None,
                "unit": "tokens",
                "restart_required": True,
                "quality_sensitive": False,
                "description": "Draft tokens speculated per step (0 disables speculation).",
            },
        )
        self.assertIn("memory.gpu-headroom-mib", by_name)
        self.assertTrue(all("models.flashnext." in n for n in by_name if n.startswith("models.")))

    def test_controls_for_a_type_without_a_schema_is_a_404(self):
        status, payload = self.get("/api/engines/vllm-r9700/controls")
        self.assertEqual(status, 404)
        self.assertIn("no control schema", payload["error"])

    # ---- preview ----------------------------------------------------------

    def test_preview_of_a_valid_candidate(self):
        status, payload = self.get(f"/api/engines/radiance-r9700/profiles/{self.CANDIDATE}/preview")
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["active_profile"], self.ACTIVE)
        self.assertEqual(payload["name"], self.CANDIDATE)
        self.assertEqual(
            payload["changes"],
            [
                {
                    "path": "models.flashnext.num-speculative-tokens",
                    "from": 5,
                    "to": 6,
                    "unit": "tokens",
                    "restart_required": True,
                    "quality_sensitive": False,
                }
            ],
        )
        self.assertEqual(payload["active_sha256"], hashlib.sha256((self.dir / self.ACTIVE).read_bytes()).hexdigest())
        self.assertEqual(
            payload["candidate_sha256"], hashlib.sha256((self.dir / self.CANDIDATE).read_bytes()).hexdigest()
        )

    def test_preview_of_the_active_profile_changes_nothing(self):
        status, payload = self.get(f"/api/engines/radiance-r9700/profiles/{self.ACTIVE}/preview")
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["changes"], [])

    def test_a_file_that_does_not_parse_is_a_200_with_valid_false(self):
        status, payload = self.get(f"/api/engines/radiance-r9700/profiles/{self.BROKEN}/preview")
        self.assertEqual(status, 200)
        self.assertFalse(payload["valid"])
        self.assertEqual(len(payload["errors"]), 1)
        self.assertIn("line 5", payload["errors"][0])
        self.assertEqual(payload["changes"], [])

    def test_preview_of_an_unknown_profile_is_a_404(self):
        status, payload = self.get("/api/engines/radiance-r9700/profiles/nope.yaml/preview")
        self.assertEqual(status, 404)
        self.assertIn("nope.yaml", payload["error"])

    def test_preview_of_a_name_that_is_not_a_profile_is_a_404(self):
        self.assertEqual(self.get("/api/engines/radiance-r9700/profiles/notes.txt/preview")[0], 404)

    def test_preview_for_a_type_without_a_schema_is_a_404(self):
        status, payload = self.get(f"/api/engines/vllm-r9700/profiles/{self.CANDIDATE}/preview")
        self.assertEqual(status, 404)
        self.assertIn("no control schema", payload["error"])

    def test_preview_for_an_unknown_engine_is_a_404(self):
        self.assertEqual(self.get("/api/engines/apollo-x/controls")[0], 404)
        self.assertEqual(self.get("/api/engines/apollo-x/profiles/x.yaml/preview")[0], 404)

    def test_preview_for_an_engine_without_profiles_dir_is_a_404(self):
        self.assertEqual(self.get("/api/engines/llamacpp-4060ti/controls")[0], 404)

    # ---- unchanged surfaces -------------------------------------------------

    def test_the_existing_profile_routes_still_answer_the_same(self):
        status, payload = self.get("/api/engines/radiance-r9700/profiles")
        self.assertEqual(status, 200)
        self.assertEqual(payload["active_profile"], self.ACTIVE)
        self.assertEqual(
            [row["name"] for row in payload["profiles"]], [self.ACTIVE, self.BROKEN, self.CANDIDATE]
        )
        status, payload = self.get(f"/api/engines/radiance-r9700/profiles/{self.CANDIDATE}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["name"], self.CANDIDATE)
        self.assertIn("num-speculative-tokens: 6", payload["text"])

    def test_unknown_sub_paths_are_still_404(self):
        for path in (
            "/api/engines/radiance-r9700/status",
            "/api/engines/radiance-r9700/profiles/a/b.yaml",
            "/api/engines/radiance-r9700/profiles/a.yaml/preview/extra",
            "/api/engines/radiance-r9700/profiles/a.yaml/apply",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_nothing_is_written_or_renamed(self):
        before = sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.iterdir())
        self.get("/api/engines/radiance-r9700/controls")
        self.get(f"/api/engines/radiance-r9700/profiles/{self.CANDIDATE}/preview")
        self.get(f"/api/engines/radiance-r9700/profiles/{self.BROKEN}/preview")
        self.assertEqual(
            sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.iterdir()), before
        )


if __name__ == "__main__":
    unittest.main()
