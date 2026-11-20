"""load_config: search order, defaults and per-entry validation."""

import os
import re
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from wsd.config import DEFAULT_CONFIG, DEFAULT_CONFIG_PATH, ENGINE_TYPES, ENV_VAR, load_config
from wsd.guardian_state import default_state_path


def write_toml(tmpdir: str, body: str) -> str:
    path = os.path.join(tmpdir, "panoptes.toml")
    with open(path, "w") as fh:
        fh.write(body)
    return path


GOOD = """
[[engines]]
id = "rad"
type = "radiance"
base_url = "http://127.0.0.1:8000"
label = "Radiance"

[[engines]]
id = "ocr"
type = "openai-compatible"
base_url = "http://localhost:8080/v1"
label = "OCR box"
"""


class LoadConfigTest(unittest.TestCase):
    def test_reads_given_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(write_toml(tmp, GOOD))
        self.assertEqual(cfg["warnings"], [])
        self.assertEqual([e["id"] for e in cfg["engines"]], ["rad", "ocr"])
        self.assertEqual(cfg["engines"][1]["base_url"], "http://localhost:8080/v1")

    def test_env_var_used_when_no_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_toml(tmp, GOOD)
            with mock.patch.dict(os.environ, {ENV_VAR: path}):
                cfg = load_config()
        self.assertEqual([e["id"] for e in cfg["engines"]], ["rad", "ocr"])

    def test_explicit_path_beats_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {ENV_VAR: write_toml(tmp, GOOD)}):
                cfg = load_config(os.path.join(tmp, "does-not-exist.toml"))
        self.assertEqual([e["id"] for e in cfg["engines"]], ["radiance-r9700"])
        self.assertIn("file not found", "\n".join(cfg["warnings"]))

    def test_missing_repo_default_is_silent(self):
        with mock.patch("wsd.config.DEFAULT_CONFIG_PATH", "/nonexistent/panoptes.toml"):
            cfg = load_config()
        self.assertEqual(cfg["warnings"], [])
        self.assertEqual(cfg["engines"], DEFAULT_CONFIG["engines"])

    def test_repo_default_file_loads_alongside_builtin_entry(self):
        if not DEFAULT_CONFIG_PATH.is_file():
            # A published tree holds no machine's live configuration, only the
            # commented example; there is nothing to load alongside the built-in
            # entry then, and test_missing_repo_default_is_silent covers that.
            self.skipTest("no config/panoptes.toml in this tree")
        cfg = load_config(DEFAULT_CONFIG_PATH)
        self.assertEqual(cfg["warnings"], [])
        default_entry = DEFAULT_CONFIG["engines"][0]
        matching = [e for e in cfg["engines"] if e["id"] == default_entry["id"]]
        # The shipped file may carry the optional read-only profile keys on top
        # of the built-in entry; everything else has to agree with it.
        self.assertLessEqual(set(matching[0]) - set(default_entry), {"profiles_dir", "active_profile"})
        self.assertEqual(
            {k: v for k, v in matching[0].items() if k not in ("profiles_dir", "active_profile")},
            default_entry,
        )
        if "active_profile" in matching[0]:
            self.assertIn("profiles_dir", matching[0])  # never half a pair
        from wsd.inference import build_engines

        engines, warnings = build_engines(cfg)
        self.assertEqual(warnings, [])
        self.assertEqual(sorted(engines), sorted(e["id"] for e in cfg["engines"]))

    def test_builtin_default_is_radiance_only(self):
        self.assertEqual([e["id"] for e in DEFAULT_CONFIG["engines"]], ["radiance-r9700"])
        self.assertEqual([e["type"] for e in DEFAULT_CONFIG["engines"]], ["radiance"])

    def test_unreadable_file_warns_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_toml(tmp, "[[engines]\nbroken")
            cfg = load_config(path)
        self.assertEqual(cfg["engines"], DEFAULT_CONFIG["engines"])
        self.assertIn("using defaults", "\n".join(cfg["warnings"]))

    def test_missing_engines_section_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(write_toml(tmp, '[other]\nx = 1\n'))
        self.assertEqual(cfg["engines"], DEFAULT_CONFIG["engines"])
        self.assertIn("no [[engines]]", "\n".join(cfg["warnings"]))


class RecordsTest(unittest.TestCase):
    """[records] dir: parsed when valid, dropped with a warning when not."""

    def load(self, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_toml(tmp, GOOD + body)
            return load_config(path)

    def test_records_dir_read(self):
        cfg = self.load('\n[records]\ndir = "/srv/pm/.pm"\n')
        self.assertEqual(cfg["records_dir"], "/srv/pm/.pm")
        self.assertEqual(cfg["warnings"], [])

    def test_records_section_absent_is_none_and_silent(self):
        cfg = self.load("")
        self.assertIsNone(cfg["records_dir"])
        self.assertEqual(cfg["warnings"], [])

    def test_records_not_a_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_toml(tmp, 'records = "/srv/pm"\n' + GOOD)  # top-level, before the tables
            cfg = load_config(path)
        self.assertIsNone(cfg["records_dir"])
        self.assertIn("records is str", "\n".join(cfg["warnings"]))

    def test_records_dir_relative_dropped(self):
        cfg = self.load('\n[records]\ndir = ".pm"\n')
        self.assertIsNone(cfg["records_dir"])
        self.assertIn("is not absolute", "\n".join(cfg["warnings"]))

    def test_records_dir_not_a_string_dropped(self):
        cfg = self.load('\n[records]\ndir = 7\n')
        self.assertIsNone(cfg["records_dir"])
        self.assertIn("not a path string", "\n".join(cfg["warnings"]))

    def test_records_dir_missing_key_dropped(self):
        cfg = self.load('\n[records]\nother = "x"\n')
        self.assertIsNone(cfg["records_dir"])
        self.assertIn("not a path string", "\n".join(cfg["warnings"]))

    def test_records_only_config_keeps_dir(self):
        # no [[engines]] section: the defaults fallback must not drop records_dir
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(write_toml(tmp, '[records]\ndir = "/srv/pm/.pm"\n'))
        self.assertEqual(cfg["records_dir"], "/srv/pm/.pm")
        self.assertEqual(cfg["engines"], DEFAULT_CONFIG["engines"])
        self.assertIn("no [[engines]]", "\n".join(cfg["warnings"]))

    def test_records_only_config_relative_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(write_toml(tmp, '[records]\ndir = ".pm"\n'))
        self.assertIsNone(cfg["records_dir"])
        self.assertIn("is not absolute", "\n".join(cfg["warnings"]))
        self.assertIn("no [[engines]]", "\n".join(cfg["warnings"]))

    def test_default_config_has_records_dir_key(self):
        with mock.patch("wsd.config.DEFAULT_CONFIG_PATH", "/nonexistent/panoptes.toml"):
            env = {k: v for k, v in os.environ.items() if k != ENV_VAR}
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = load_config()
        self.assertIsNone(cfg["records_dir"])


class GuardianStateFileTest(unittest.TestCase):
    """[guardian] state_file: the file to read, or the default with a warning."""

    def load(self, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            return load_config(write_toml(tmp, GOOD + body))

    def test_state_file_read(self):
        cfg = self.load('\n[guardian]\nstate_file = "/srv/state/panoptes/guardian/state.json"\n')
        self.assertEqual(cfg["guardian_state_file"], "/srv/state/panoptes/guardian/state.json")
        self.assertEqual(cfg["warnings"], [])

    def test_guardian_section_absent_is_the_default_and_silent(self):
        cfg = self.load("")
        self.assertEqual(cfg["guardian_state_file"], default_state_path())
        self.assertEqual(cfg["warnings"], [])

    def test_state_file_relative_dropped(self):
        cfg = self.load('\n[guardian]\nstate_file = "state.json"\n')
        self.assertEqual(cfg["guardian_state_file"], default_state_path())
        self.assertIn("is not absolute", "\n".join(cfg["warnings"]))

    def test_state_file_missing_key_is_the_default(self):
        cfg = self.load('\n[guardian]\nother = "x"\n')
        self.assertEqual(cfg["guardian_state_file"], default_state_path())
        self.assertIn("state_file None is not a path string", "\n".join(cfg["warnings"]))

    def test_state_file_not_a_string(self):
        cfg = self.load("\n[guardian]\nstate_file = 7\n")
        self.assertEqual(cfg["guardian_state_file"], default_state_path())
        self.assertIn("state_file 7 is not a path string", "\n".join(cfg["warnings"]))

    def test_state_file_that_is_a_directory_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.load(f'\n[guardian]\nstate_file = "{tmp}"\n')
        self.assertEqual(cfg["guardian_state_file"], default_state_path())
        self.assertIn("is a directory", "\n".join(cfg["warnings"]))

    def test_guardian_not_a_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(write_toml(tmp, 'guardian = "/tmp/state.json"\n' + GOOD))
        self.assertEqual(cfg["guardian_state_file"], default_state_path())
        self.assertIn("guardian is str", "\n".join(cfg["warnings"]))

    def test_guardian_only_config_keeps_the_state_file(self):
        # no [[engines]] section: the defaults fallback must not drop it either
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(write_toml(tmp, '[guardian]\nstate_file = "/srv/state/g.json"\n'))
        self.assertEqual(cfg["guardian_state_file"], "/srv/state/g.json")
        self.assertEqual(cfg["engines"], DEFAULT_CONFIG["engines"])
        self.assertIn("no [[engines]]", "\n".join(cfg["warnings"]))

    def test_default_config_has_the_default_state_file(self):
        with mock.patch("wsd.config.DEFAULT_CONFIG_PATH", "/nonexistent/panoptes.toml"):
            env = {k: v for k, v in os.environ.items() if k != ENV_VAR}
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = load_config()
        self.assertEqual(cfg["guardian_state_file"], default_state_path())


class ValidationTest(unittest.TestCase):
    def load(self, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            return load_config(write_toml(tmp, body))

    def entry(self, **over):
        base = {"id": "e1", "type": "radiance", "base_url": "http://127.0.0.1:8000"}
        base.update(over)
        lines = ["[[engines]]"]
        for k, v in base.items():
            lines.append(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}")
        return self.load("\n".join(lines) + "\n")

    def test_label_defaults_to_id(self):
        self.assertEqual(self.entry()["engines"][0]["label"], "e1")

    def test_unknown_type_rejected(self):
        cfg = self.entry(type="ollama")
        self.assertEqual(cfg["engines"], [])
        self.assertIn("unknown type", "\n".join(cfg["warnings"]))

    def test_missing_id_rejected(self):
        cfg = self.load('[[engines]]\ntype = "radiance"\nbase_url = "http://127.0.0.1:1"\n')
        self.assertEqual(cfg["engines"], [])
        self.assertIn("no id", "\n".join(cfg["warnings"]))

    def test_duplicate_ids_second_skipped(self):
        cfg = self.load(GOOD.replace('"ocr"', '"rad"'))
        self.assertEqual([e["id"] for e in cfg["engines"]], ["rad"])
        self.assertIn("duplicate", "\n".join(cfg["warnings"]))

    def test_non_loopback_host_rejected(self):
        cfg = self.entry(base_url="http://192.168.1.20:8000")
        self.assertEqual(cfg["engines"], [])
        self.assertIn("not loopback", "\n".join(cfg["warnings"]))

    def test_non_http_scheme_rejected(self):
        cfg = self.entry(base_url="ftp://127.0.0.1:21")
        self.assertEqual(cfg["engines"], [])
        self.assertIn("not http(s)", "\n".join(cfg["warnings"]))

    def test_missing_scheme_rejected(self):
        cfg = self.entry(base_url="127.0.0.1:8000")
        self.assertEqual(cfg["engines"], [])

    def test_loopback_variants_accepted(self):
        for url in (
            "http://localhost:8000",
            "http://127.0.0.53:8000",
            "https://127.1.2.3:8443",
            "https://[::1]:8000",
        ):
            with self.subTest(url=url):
                self.assertEqual(len(self.entry(base_url=url)["engines"]), 1)

    def test_engines_not_a_list_warns(self):
        cfg = self.load("engines = 3\n")
        self.assertEqual(cfg["engines"], [])
        self.assertIn("expected a list", "\n".join(cfg["warnings"]))

    # profiles_dir / active_profile are optional and machine-specific: a bad
    # value costs the keys and a warning, never the engine.

    def test_no_profile_keys_is_the_normal_case(self):
        cfg = self.entry()
        self.assertEqual(sorted(cfg["engines"][0]), ["base_url", "id", "label", "type"])
        self.assertEqual(cfg["warnings"], [])

    def profiles_entry(self, **over):
        """One engine whose profiles_dir really exists and holds one profile;
        ``over`` replaces either profile key."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "flashnext.yaml").write_text("ctx: 8192\n")
            entry = {"profiles_dir": tmp, "active_profile": "flashnext.yaml"}
            return self.entry(**{**entry, **over})

    def test_usable_profile_keys_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "flashnext.yaml").write_text("ctx: 8192\n")
            cfg = self.entry(profiles_dir=tmp, active_profile="flashnext.yaml")
            self.assertEqual(cfg["warnings"], [])
            self.assertEqual(cfg["engines"][0]["profiles_dir"], tmp)
            self.assertEqual(cfg["engines"][0]["active_profile"], "flashnext.yaml")

    def test_yml_active_profile_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "legacy.yml").write_text("ctx: 8192\n")
            cfg = self.entry(profiles_dir=tmp, active_profile="legacy.yml")
            self.assertEqual(cfg["engines"][0]["active_profile"], "legacy.yml")
            self.assertEqual(cfg["warnings"], [])

    def test_active_profile_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.entry(profiles_dir=tmp)
            self.assertEqual(cfg["engines"][0]["profiles_dir"], tmp)
            self.assertNotIn("active_profile", cfg["engines"][0])
            self.assertEqual(cfg["warnings"], [])

    def test_missing_profiles_dir_drops_both_keys(self):
        cfg = self.entry(profiles_dir="/nonexistent/profiles", active_profile="flashnext.yaml")
        self.assertNotIn("profiles_dir", cfg["engines"][0])
        self.assertNotIn("active_profile", cfg["engines"][0])
        self.assertIn("is not a directory", "\n".join(cfg["warnings"]))
        self.assertIn("without a usable profiles_dir", "\n".join(cfg["warnings"]))

    def test_relative_profiles_dir_drops_both_keys(self):
        cfg = self.profiles_entry(profiles_dir="Applications/Radiance")
        self.assertNotIn("profiles_dir", cfg["engines"][0])
        self.assertIn("is not absolute", "\n".join(cfg["warnings"]))

    def test_profiles_dir_on_a_file_drops_both_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.entry(profiles_dir=os.path.join(tmp, "panoptes.toml"))
        self.assertNotIn("profiles_dir", cfg["engines"][0])
        self.assertIn("is not a directory", "\n".join(cfg["warnings"]))

    def test_non_string_profiles_dir_drops_the_keys_but_keeps_the_engine(self):
        cfg = self.profiles_entry(profiles_dir=7)
        self.assertEqual(len(cfg["engines"]), 1)
        self.assertNotIn("profiles_dir", cfg["engines"][0])
        self.assertIn("not a path string", "\n".join(cfg["warnings"]))

    def test_active_profile_naming_a_missing_file_is_dropped(self):
        cfg = self.profiles_entry(active_profile="absent.yaml")
        self.assertIn("profiles_dir", cfg["engines"][0])
        self.assertNotIn("active_profile", cfg["engines"][0])
        self.assertIn("is not a file in", "\n".join(cfg["warnings"]))

    def test_active_profile_that_is_a_path_is_dropped(self):
        cfg = self.profiles_entry(active_profile="../flashnext.yaml")
        self.assertIn("profiles_dir", cfg["engines"][0])
        self.assertNotIn("active_profile", cfg["engines"][0])
        self.assertIn("is not a *.yaml/*.yml file name", "\n".join(cfg["warnings"]))


class HarnessValidationTest(unittest.TestCase):
    """[[harnesses]] entries: same treatment as engines — broken ones are
    skipped with a warning, never fatal."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()  # absolute existing dir, outlives load()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def load(self, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            return load_config(write_toml(tmp, body))

    def entry(self, harness_body: str) -> dict:
        """Load the harness table on top of a valid engines section so the
        only warnings under test concern harnesses."""
        return self.load(GOOD + "\n" + harness_body)

    def harness(self, **over) -> dict:
        base = {"id": "omp-local", "type": "omp", "sessions_dir": self.dir}
        base.update(over)
        lines = ["[[harnesses]]"]
        for k, v in base.items():
            lines.append(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}")
        return self.entry("\n".join(lines) + "\n")

    def test_valid_entry(self):
        cfg = self.harness(lock_file="/var/lib/lab01/worker-active", label="Worker 1")
        self.assertEqual(cfg["harnesses"], [{
            "id": "omp-local", "type": "omp", "sessions_dir": self.dir,
            "label": "Worker 1", "lock_file": "/var/lib/lab01/worker-active",
        }])
        self.assertEqual(cfg["warnings"], [])

    def test_label_defaults_to_id(self):
        self.assertEqual(self.harness()["harnesses"][0]["label"], "omp-local")

    def test_no_harnesses_section(self):
        cfg = self.load(GOOD)
        self.assertEqual(cfg["harnesses"], [])
        self.assertEqual(cfg["warnings"], [])

    def test_unknown_type_skipped(self):
        cfg = self.harness(type="claude-code")
        self.assertEqual(cfg["harnesses"], [])
        self.assertIn("unknown type", "\n".join(cfg["warnings"]))

    def test_entry_without_id_skipped(self):
        cfg = self.load(f'[[harnesses]]\ntype = "omp"\nsessions_dir = "{self.dir}"\n')
        self.assertEqual(cfg["harnesses"], [])
        self.assertIn("has no id", "\n".join(cfg["warnings"]))

    def test_missing_sessions_dir_skips_the_entry(self):
        cfg = self.harness(sessions_dir=os.path.join(self.dir, "absent"))
        self.assertEqual(cfg["harnesses"], [])
        self.assertIn("not an existing absolute directory", "\n".join(cfg["warnings"]))

    def test_relative_sessions_dir_skips_the_entry(self):
        cfg = self.harness(sessions_dir="relative/dir")
        self.assertEqual(cfg["harnesses"], [])
        self.assertIn("not an existing absolute directory", "\n".join(cfg["warnings"]))

    def test_relative_lock_file_is_dropped_not_fatal(self):
        cfg = self.harness(lock_file="state/worker-active")
        self.assertEqual(len(cfg["harnesses"]), 1)
        self.assertNotIn("lock_file", cfg["harnesses"][0])
        self.assertIn("not an absolute path", "\n".join(cfg["warnings"]))

    def test_duplicate_id_skipped(self):
        body = (
            f'[[harnesses]]\nid = "dup"\ntype = "omp"\nsessions_dir = "{self.dir}"\n'
            f'[[harnesses]]\nid = "dup"\ntype = "omp"\nsessions_dir = "{self.dir}"\n'
        )
        cfg = self.load(body)
        self.assertEqual(len(cfg["harnesses"]), 1)
        self.assertIn("duplicate id", "\n".join(cfg["warnings"]))

    def test_harnesses_must_be_a_list(self):
        cfg = self.load('harnesses = "omp"\n')
        self.assertEqual(cfg["harnesses"], [])
        self.assertIn("expected a list", "\n".join(cfg["warnings"]))

    def test_harnesses_survive_missing_engines_section(self):
        cfg = self.load(
            f'[[harnesses]]\nid = "h"\ntype = "omp"\nsessions_dir = "{self.dir}"\n'
        )
        self.assertEqual(len(cfg["harnesses"]), 1)
        self.assertIn("no [[engines]]", "\n".join(cfg["warnings"]))


class ExampleConfigTest(unittest.TestCase):
    """config/panoptes.example.toml: what ships instead of this machine's config.

    It must parse, must carry every key the code reads, and must be neutral:
    validating it may only complain that the placeholder paths do not exist —
    never that a value is wrong — and it must name no real home directory.
    """

    EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "panoptes.example.toml"

    #: the only kind of complaint a placeholder path may cause
    MISSING_PATH = (
        "is not a directory",
        "is not a file in",
        "is not an existing absolute directory",
        "without a usable profiles_dir",
    )

    def setUp(self):
        self.assertTrue(self.EXAMPLE.is_file(), f"missing {self.EXAMPLE}")
        self.text = self.EXAMPLE.read_text(encoding="utf-8")
        self.raw = tomllib.loads(self.text)

    def test_parses_and_names_every_section_the_code_reads(self):
        self.assertEqual(set(self.raw), {"engines", "harnesses", "records", "guardian"})
        engine = self.raw["engines"][0]
        for key in ("id", "type", "base_url", "label", "profiles_dir",
                    "active_profile", "journal_dir"):
            self.assertIn(key, engine)
        harness = self.raw["harnesses"][0]
        for key in ("id", "type", "label", "sessions_dir", "lock_file"):
            self.assertIn(key, harness)
        self.assertIn("dir", self.raw["records"])
        self.assertIn("state_file", self.raw["guardian"])

    def test_validation_only_misses_the_placeholder_paths(self):
        cfg = load_config(str(self.EXAMPLE))
        for warning in cfg["warnings"]:
            self.assertTrue(any(phrase in warning for phrase in self.MISSING_PATH), warning)
        # the engines themselves are valid; only their paths are placeholders
        self.assertEqual([e["id"] for e in cfg["engines"]],
                         [e["id"] for e in self.raw["engines"]])
        self.assertEqual(cfg["records_dir"], self.raw["records"]["dir"])
        self.assertEqual(cfg["guardian_state_file"], self.raw["guardian"]["state_file"])

    def test_no_real_home_directory_and_only_loopback_urls(self):
        users = {m.group(1) for m in re.finditer(r"/home/([^/\s\"']+)", self.text)}
        self.assertEqual(users - {"YOUR_USER"}, set())
        self.assertEqual({e["id"] for e in self.raw["engines"]},
                         {"radiance-main", "llamacpp-second"})
        for engine in self.raw["engines"]:
            self.assertIn(engine["type"], ENGINE_TYPES)
            self.assertIn(urlparse(engine["base_url"]).hostname, ("localhost", "127.0.0.1", "::1"))
        for key, value in {**self.raw["records"], **self.raw["guardian"]}.items():
            self.assertTrue(Path(value).is_absolute(), f"{key} is not absolute")


if __name__ == "__main__":
    unittest.main()
