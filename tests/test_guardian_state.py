"""The Guardian state file reader and the read-only route in front of it.

Every one of the seven display rules of GUARDIAN.md §9 is checked against a
fixture file in a temp directory: no real Guardian process, no real clock (the
time and the pid check are arguments) and nothing under ~/.local/state.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from wsd import guardian_state, routes, server
from wsd.config import load_config

NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
FRESH = "2026-11-20T12:00:00Z"


def alive(pid: object) -> bool:
    """The tests own the process table: every pid they invent is running."""
    return True


def state_doc(**over) -> dict:
    """A well-formed v1 state document, with the named fields replaced."""
    doc = {
        "schema": guardian_state.SCHEMA_V1,
        "written_at": FRESH,
        "service": {"version": "0.1.0", "pid": 4242, "mode": "simulate"},
        "state": "GREEN",
        "estop": {"latched": False, "since": None, "reason": None, "scope": None},
        "rules_by_state": {"GREEN": 14, "AMBER": 0, "ORANGE": 0, "RED": 0, "UNKNOWN": 1},
        "top": [],
        "open_incident": None,
    }
    doc.update(over)
    return doc


class StateFileCase(unittest.TestCase):
    """A temp dir holding one state file, written as text or as JSON."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "state.json")

    def write(self, doc) -> str:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return self.path

    def write_text(self, text: str) -> str:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return self.path

    def read(self, now=NOW, pid_alive=alive, path=None) -> dict:
        return guardian_state.read_state(path or self.path, now, pid_alive=pid_alive)


class Rule1MissingTest(StateFileCase):
    def test_missing_file_is_not_installed(self):
        result = self.read()
        self.assertFalse(result["installed"])
        self.assertIsNone(result["state"])
        self.assertEqual(
            result["display"],
            {"level": "off", "label": "Guardian · not installed", "detail": ""},
        )

    def test_reading_creates_nothing(self):
        missing = str(Path(self._tmp.name) / "nope" / "guardian" / "state.json")
        self.read(path=missing)
        self.assertFalse(Path(self._tmp.name, "nope").exists())

    def test_a_directory_there_is_no_state_file(self):
        """Rule 1 asks for a file: a directory in that place is not one."""
        Path(self.path).mkdir()
        result = self.read()
        self.assertFalse(result["installed"])
        self.assertEqual(result["display"]["level"], "off")
        self.assertEqual(result["display"]["label"], "Guardian · not installed")


class Rule2UnreadableTest(StateFileCase):
    def test_not_json(self):
        self.write_text("{ this is not json")
        result = self.read()
        self.assertTrue(result["installed"])
        self.assertIsNone(result["state"])
        self.assertEqual(
            result["display"], {"level": "warn", "label": "Guardian · unreadable", "detail": ""}
        )
        self.assertIn("not JSON", "\n".join(result["warnings"]))

    def test_json_but_not_an_object(self):
        self.write_text('["GREEN", "AMBER"]')
        result = self.read()
        self.assertEqual(result["display"]["label"], "Guardian · unreadable")
        self.assertIsNone(result["state"])

    def test_empty_file(self):
        self.write_text("")
        self.assertEqual(self.read()["display"]["label"], "Guardian · unreadable")

    def test_unreadable_file(self):
        self.write(state_doc())
        with mock.patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            result = self.read()
        self.assertEqual(result["display"]["label"], "Guardian · unreadable")
        self.assertIn("could not be read", "\n".join(result["warnings"]))


class Rule3SchemaTest(StateFileCase):
    def test_unknown_schema(self):
        self.write(state_doc(schema="panoptes.guardian.state.v2"))
        result = self.read()
        self.assertEqual(
            result["display"], {"level": "off", "label": "Guardian · unsupported version", "detail": ""}
        )
        # the file itself is still reported verbatim, and its state is never shown
        self.assertEqual(result["state"]["state"], "GREEN")
        self.assertIn("v2", "\n".join(result["warnings"]))

    def test_missing_schema(self):
        doc = state_doc()
        del doc["schema"]
        self.write(doc)
        self.assertEqual(self.read()["display"]["label"], "Guardian · unsupported version")


class Rule4FreshnessTest(StateFileCase):
    def test_missing_written_at(self):
        doc = state_doc()
        del doc["written_at"]
        self.write(doc)
        result = self.read()
        self.assertEqual(result["display"]["level"], "warn")
        self.assertEqual(result["display"]["label"], "Guardian · not responding")

    def test_unparseable_written_at(self):
        self.write(state_doc(written_at="20 November 2026, midday"))
        self.assertEqual(self.read()["display"]["label"], "Guardian · not responding")

    def test_fresh_at_exactly_the_boundary(self):
        """STALE_AFTER_S is exclusive: age == 10.0 s is still alive, so a
        2 s writer must miss five samples in a row before the chip complains."""
        self.write(state_doc(written_at="2026-11-20T11:59:50Z"))
        result = self.read()
        self.assertEqual((result["state"]["state"], result["display"]["level"]), ("GREEN", "ok"))

    def test_stale_just_past_the_boundary(self):
        self.write(state_doc(written_at="2026-11-20T11:59:49.9Z"))
        result = self.read(now=NOW)
        self.assertEqual(result["display"]["level"], "warn")
        self.assertEqual(result["display"]["label"], "Guardian · not responding")
        self.assertIn("older than 10", "\n".join(result["warnings"]))

    def test_a_stale_colour_is_never_shown(self):
        self.write(state_doc(state="RED", written_at="2026-11-20T11:30:00Z"))
        self.assertNotIn("RED", self.read()["display"]["label"])

    def test_dead_pid_with_a_fresh_stamp(self):
        self.write(state_doc(state="GREEN"))
        result = self.read(pid_alive=lambda pid: False)
        self.assertEqual(result["display"]["level"], "warn")
        self.assertEqual(result["display"]["label"], "Guardian · not responding")
        self.assertIn("4242", "\n".join(result["warnings"]))

    def test_naive_written_at_is_read_as_utc(self):
        self.write(state_doc(written_at="2026-11-20T11:59:55"))
        self.assertEqual(self.read()["display"]["level"], "ok")

    def test_the_pid_check_sees_the_file_pid(self):
        seen = []

        def spy(pid):
            seen.append(pid)
            return True

        self.write(state_doc())
        self.read(pid_alive=spy)
        self.assertEqual(seen, [4242])


class Rule5EStopTest(StateFileCase):
    def test_latched_estop_overrides_a_green_state(self):
        self.write(state_doc(state="GREEN", estop={"latched": True, "scope": "worker-7"}))
        result = self.read()
        self.assertEqual(result["display"]["level"], "hot")
        self.assertEqual(result["display"]["label"], "E-STOP · worker-7")

    def test_any_configured_scope_is_named(self):
        """The scope is a worker id from the Guardian config, or "all"; the
        product knows no ids and names whichever non-empty one it is given."""
        for scope in ("worker-7", "build-agent-2", "bench-3", "all"):
            with self.subTest(scope=scope):
                self.write(state_doc(estop={"latched": True, "scope": scope}))
                self.assertEqual(self.read()["display"]["label"], f"E-STOP · {scope}")

    def test_all_is_an_ordinary_scope_with_no_warning(self):
        self.write(state_doc(estop={"latched": True, "scope": "all"}))
        result = self.read()
        self.assertEqual(result["display"]["label"], "E-STOP · all")
        self.assertEqual(result["warnings"], [])

    def test_a_long_scope_is_cut_at_the_cap(self):
        scope = "w" * (guardian_state.SCOPE_MAX + 8)
        self.write(state_doc(estop={"latched": True, "scope": scope}))
        shown = self.read()["display"]["label"].split(" · ", 1)[1]
        self.assertEqual(shown, "w" * guardian_state.SCOPE_MAX)

    def test_markup_shaped_scope_is_shown_verbatim(self):
        """Nothing here escapes or interprets the scope: the label is plain text
        for the browser's `textContent`, so markup stays markup."""
        scope = "<b>all</b>"
        self.write(state_doc(estop={"latched": True, "scope": scope}))
        self.assertEqual(self.read()["display"]["label"], f"E-STOP · {scope}")

    def test_a_scope_that_is_not_a_non_empty_string_is_said_but_not_shown(self):
        for scope in (None, "", "   ", 7, ["all"]):
            with self.subTest(scope=scope):
                self.write(state_doc(estop={"latched": True, "scope": scope}))
                result = self.read()
                self.assertEqual(result["display"]["label"], "E-STOP")
                self.assertIn("estop.scope", "\n".join(result["warnings"]))

    def test_latched_and_stale_is_not_responding(self):
        """Rule 4 runs first: a dead service cannot report a latch we can trust."""
        self.write(state_doc(written_at="2026-11-20T11:00:00Z", estop={"latched": True, "scope": "all"}))
        self.assertEqual(self.read()["display"]["label"], "Guardian · not responding")

    def test_a_non_latched_estop_does_not_show(self):
        self.write(state_doc(estop={"latched": False, "scope": "all"}))
        self.assertEqual(self.read()["display"]["level"], "ok")

    def test_a_latched_estop_keeps_the_detail(self):
        self.write(state_doc(
            estop={"latched": True, "scope": "worker-7"},
            top=[{"rule": "gpu.nvidia.core", "state": "RED", "value": 91.0, "unit": "C", "threshold": 90.0}],
            open_incident={"id": "INC-12"},
        ))
        self.assertEqual(
            self.read()["display"]["detail"],
            "gpu.nvidia.core 91 C (90); incident INC-12",
        )


class Rule6StateTest(StateFileCase):
    def test_level_per_state(self):
        for state, level in (("GREEN", "ok"), ("AMBER", "warn"), ("ORANGE", "warn"),
                             ("RED", "hot"), ("UNKNOWN", "unknown")):
            with self.subTest(state=state):
                self.write(state_doc(state=state, service={"pid": 1, "mode": "live"}))
                result = self.read()
                self.assertEqual(result["display"]["level"], level)
                self.assertEqual(result["display"]["label"], f"Guardian · {state}")

    def test_simulate_gets_the_sim_suffix(self):
        self.write(state_doc(state="AMBER", service={"pid": 1, "mode": "simulate"}))
        self.assertEqual(self.read()["display"]["label"], "Guardian · AMBER (sim)")

    def test_live_has_no_suffix(self):
        self.write(state_doc(state="AMBER", service={"pid": 1, "mode": "live"}))
        self.assertNotIn("(sim)", self.read()["display"]["label"])

    def test_a_missing_mode_is_said_but_unsuffixed(self):
        self.write(state_doc(service={"pid": 1}))
        result = self.read()
        self.assertEqual(result["display"]["label"], "Guardian · GREEN")
        self.assertIn("service.mode", "\n".join(result["warnings"]))

    def test_a_state_that_is_not_one_of_the_five_is_unknown(self):
        self.write(state_doc(state="CERTAINLY-BROKEN", service={"pid": 1, "mode": "live"}))
        result = self.read()
        self.assertEqual(result["display"]["level"], "unknown")
        self.assertEqual(result["display"]["label"], "Guardian · CERTAINLY-BROKEN")
        self.assertIn("is not one of", "\n".join(result["warnings"]))

    def test_a_missing_state_is_unknown(self):
        doc = state_doc()
        del doc["state"]
        doc["service"] = {"pid": 1, "mode": "live"}
        self.write(doc)
        result = self.read()
        self.assertEqual(result["display"]["level"], "unknown")
        self.assertEqual(result["display"]["label"], "Guardian · UNKNOWN")


class Rule7DetailTest(StateFileCase):
    def test_full_rule_and_incident(self):
        self.write(state_doc(
            top=[
                {"rule": "gpu.r9700.junction", "state": "AMBER", "value": 96.0, "unit": "C", "threshold": 95.0},
                {"rule": "cpu.power", "state": "AMBER", "value": 138.5, "unit": "W", "threshold": 120.0},
            ],
            open_incident={"id": "INC-3", "opened_at": FRESH},
        ))
        self.assertEqual(
            self.read()["display"]["detail"],
            "gpu.r9700.junction 96 C (95); cpu.power 138.5 W (120); incident INC-3",
        )

    def test_missing_fields_are_left_out_not_invented(self):
        self.write(state_doc(top=[
            {"rule": "cpu.tctl", "value": 86.4},
            {"rule": "nvme.composite", "unit": "C"},
            {"rule": "memory.psi"},
            {"value": 12.0},
            "not a rule",
        ]))
        self.assertEqual(
            self.read()["display"]["detail"], "cpu.tctl 86.4; nvme.composite C; memory.psi"
        )

    def test_no_top_and_no_incident_is_no_detail(self):
        self.assertEqual(self.read()["display"]["detail"], "")

    def test_an_incident_without_an_id_adds_nothing(self):
        self.write(state_doc(open_incident={"opened_at": FRESH}))
        self.assertEqual(self.read()["display"]["detail"], "")

    def test_top_that_is_not_a_list_is_ignored(self):
        self.write(state_doc(top="gpu.r9700.junction 96 C"))
        self.assertEqual(self.read()["display"]["detail"], "")


class PathSourceTest(unittest.TestCase):
    """The state file comes from the config, then $XDG_STATE_HOME, then ~/.local/state."""

    def test_config_path_is_the_one_that_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp, "panoptes.toml")
            cfg.write_text(
                "[[engines]]\nid = \"rad\"\ntype = \"radiance\"\n"
                "base_url = \"http://127.0.0.1:8000\"\n"
                "\n[guardian]\n"
                f'state_file = "{tmp}/guardian/state.json"\n',
                encoding="utf-8",
            )
            path = load_config(str(cfg))["guardian_state_file"]
            self.write_to(path, state_doc(state="ORANGE"))
            result = guardian_state.read_state(path, NOW, pid_alive=alive)
        self.assertTrue(result["installed"])
        self.assertEqual(result["display"]["label"], "Guardian · ORANGE (sim)")

    def test_xdg_state_home_wins(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/srv/state", "HOME": "/home/alice"}):
            self.assertEqual(
                guardian_state.default_state_path(), "/srv/state/panoptes/guardian/state.json"
            )

    def test_empty_xdg_state_home_falls_back_to_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "", "HOME": "/home/alice"}):
            self.assertEqual(
                guardian_state.default_state_path(),
                "/home/alice/.local/state/panoptes/guardian/state.json",
            )

    def test_the_default_path_is_the_one_read_when_none_is_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp, "HOME": tmp}):
                target = Path(guardian_state.default_state_path())
                self.write_to(str(target), state_doc(state="RED"))
                result = guardian_state.read_state(None, NOW, pid_alive=alive)
        self.assertEqual(result["display"]["label"], "Guardian · RED (sim)")

    @staticmethod
    def write_to(path: str, doc: dict) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(doc), encoding="utf-8")


class PidAliveTest(unittest.TestCase):
    def test_our_own_pid_is_alive(self):
        self.assertTrue(guardian_state.pid_is_alive(os.getpid()))

    def test_process_lookup_error_is_dead(self):
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(guardian_state.pid_is_alive(4242))

    def test_permission_error_is_alive(self):
        """A process owned by somebody else is still a running process."""
        with mock.patch("os.kill", side_effect=PermissionError):
            self.assertTrue(guardian_state.pid_is_alive(4242))

    def test_a_pid_too_large_to_signal_is_not_alive(self):
        self.assertFalse(guardian_state.pid_is_alive(4_000_000_000))

    def test_things_that_are_not_pids_are_not_alive(self):
        for pid in (None, "", "4242", 0, -1, True):
            with self.subTest(pid=pid):
                self.assertFalse(guardian_state.pid_is_alive(pid))


class RouteTest(StateFileCase):
    """GET /api/guardian/state answers from the file and creates nothing."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(routes, "GUARDIAN_STATE_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_file_answers_not_installed(self):
        payload = routes.guardian_status()
        self.assertFalse(payload["installed"])
        self.assertEqual(payload["display"]["level"], "off")
        self.assertEqual(payload["display"]["label"], "Guardian · not installed")

    def test_the_route_never_creates_the_directory(self):
        routes.GUARDIAN_STATE_FILE = str(Path(self._tmp.name) / "absent-dir" / "state.json")
        payload = routes.guardian_status()
        self.assertFalse(payload["installed"])
        self.assertFalse(Path(self._tmp.name, "absent-dir").exists())

    def test_a_fresh_file_answers_its_state(self):
        """The route reads on the real clock and with the real pid check, so the
        fixture stamps itself now and names this very process."""
        self.write(state_doc(
            state="AMBER",
            service={"version": "0.1.0", "pid": os.getpid(), "mode": "simulate"},
            written_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            top=[{"rule": "gpu.r9700.junction", "value": 96.0, "unit": "C", "threshold": 95.0}],
        ))
        payload = routes.guardian_status()
        self.assertTrue(payload["installed"])
        self.assertEqual(payload["display"]["label"], "Guardian · AMBER (sim)")
        self.assertEqual(payload["display"]["detail"], "gpu.r9700.junction 96 C (95)")
        self.assertEqual(payload["state"]["state"], "AMBER")

    def test_a_stale_file_answers_not_responding(self):
        self.write(state_doc(
            state="RED",
            service={"pid": os.getpid(), "mode": "live"},
            written_at="2020-01-01T00:00:00Z",
        ))
        payload = routes.guardian_status()
        self.assertEqual(payload["display"]["label"], "Guardian · not responding")
        self.assertNotIn("RED", payload["display"]["label"])
        self.assertEqual(payload["state"]["state"], "RED")

    def test_the_payload_shape(self):
        self.assertEqual(set(routes.guardian_status()), {"installed", "display", "state", "warnings"})
        self.assertEqual(set(routes.guardian_status()["display"]), {"level", "label", "detail"})

    def test_the_route_is_registered(self):
        self.assertIn("/api/guardian/state", server.ROUTES)


if __name__ == "__main__":
    unittest.main()
