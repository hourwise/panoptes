"""The release scan: every detector fires, the allow list behaves (T-048).

``tools/release_scan.py`` is the gate in front of a public export, so both
halves are tested: each detector must find a planted example in a throw-away
product tree, and the allow list must let through exactly what it names —
never an entry without a reason, never one that matches nothing. The
machine-specific names the scanner looks for live in a local list file, so they
are planted as a file and patched in, never written out here. The last test runs
the scan over this repository's real product set.
"""

import getpass
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import release_scan
from tools.release_scan import (
    PRIVATE_DIR,
    PRIVATE_LIST,
    PRODUCT_DIRS,
    iter_files,
    load_allow,
    scan,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST = socket.gethostname()

#: Names invented for these tests. The scanner keeps the names it looks for in a
#: local list outside the code, so no real name of ours is written here: the list
#: is planted as a file in a temp tree, and the login and host names of the scan
#: are patched to these.
PLANT_NAMES = ("zephonia", "kettle-9")
PLANT_LOGIN = "zorquim"
PLANT_HOST = "zandberg"


def tree(root: Path, files: dict[str, str]) -> Path:
    """Write ``{relative path: text}`` under ``root`` and return it."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


class DetectorTests(unittest.TestCase):
    """One planted example per detector, in a temp tree shaped like the product."""

    PLANTED = {
        "wsd/home.txt": "the config sits in /home/morgan/projects/app/app.py\n",
        "web/host.txt": f"served from {PLANT_HOST} on the desk\n",
        "web/lan.txt": "the printer answers at printer.local too\n",
        "docs/mail.txt": "write to dana@widgets.internal for the keys\n",
        "tools/secret.txt": 'api_key = "Zk9xT7loopQ4"\n',
        "ops/serial.txt": "serial_number = A1B2C3D4E5F6A7B8\n",
        # the machine-specific names arrive the way the scanner gets them for
        # real: a list file in the tree, and the login name of the run
        PRIVATE_LIST: "# planted names\n" + "\n".join(PLANT_NAMES) + "\n",
        "tests/private.txt": f"copied from the {PLANT_NAMES[0]} checkout\n",
        "docs/author.txt": f"signed by {PLANT_LOGIN} at the desk\n",
        "wsd/pointer.txt": f"the task packets live in {PRIVATE_DIR}/tasks\n",
    }

    #: of those, the files the scan reads: the name list is its input, not a
    #: product file, and must never appear in the findings or the file count
    SCANNED = {rel for rel in PLANTED if rel != PRIVATE_LIST}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for name, value in (("host_names", [PLANT_HOST]), ("login_name", PLANT_LOGIN)):
            patcher = mock.patch.object(release_scan, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = tree(Path(self._tmp.name), self.PLANTED)

    def kinds(self, rel: str) -> set[str]:
        findings, _ = scan(self.root, self.root / "no-such-allow.txt")
        return {line.split(": ", 1)[1] for line in findings if line.startswith(f"{rel}:")}

    def test_each_detector_fires(self):
        for rel, kind in [
            ("wsd/home.txt", "home-path"),
            ("web/host.txt", "hostname"),
            ("web/lan.txt", "hostname"),
            ("docs/mail.txt", "email"),
            ("tools/secret.txt", "secret"),
            ("ops/serial.txt", "serial"),
            ("tests/private.txt", "private-name"),
            ("docs/author.txt", "username"),
            ("wsd/pointer.txt", "private-path"),
        ]:
            with self.subTest(file=rel):
                self.assertIn(kind, self.kinds(rel))

    def test_only_the_planted_lines_are_reported(self):
        findings, count = scan(self.root, self.root / "no-such-allow.txt")
        # the private list the scan reads is input, not a product file: it is
        # neither scanned nor counted, so the reported files are the rest
        self.assertEqual({line.split(":")[0] for line in findings}, self.SCANNED, findings)
        self.assertEqual(count, len(self.SCANNED))
        for line in findings:
            self.assertRegex(line, r"^[^:]+:[0-9]+: [a-z-]+$")

    def test_no_secret_value_is_ever_echoed(self):
        findings, _ = scan(self.root, self.root / "no-such-allow.txt")
        joined = "\n".join(findings)
        self.assertNotIn("Zk9xT7loopQ4", joined)
        self.assertNotIn("A1B2C3D4E5F6A7B8", joined)

    def test_placeholders_and_examples_are_clean(self):
        clean = {
            "wsd/ok.txt": (
                "/home/YOUR_USER/.local/state/lab01/worker-active and /home/alice/app "
                "and /home/bob/x and /home/user/y and ~/.local/state/panoptes\n"
            ),
            "web/ok.txt": 'mail you@example.com, ops@example.org, me@localhost, '
                          'api_key = os.environ["K"], password = «redacted»,\n'
                          "and output_tokens: 137881, timings_per_token = false\n",
            "docs/ok.txt": "the file is /path/to/engine/profiles, never a serial = 12, "
                           f"a philosophy note and the {PRIVATE_DIR} tree named without a slash\n",
        }
        root = tree(Path(self._tmp.name) / "clean", clean)
        findings, _ = scan(root, root / "no-such-allow.txt")
        self.assertEqual(findings, [])

    def test_the_live_config_is_not_part_of_the_product_set(self):
        listed = iter_files()
        self.assertIn("config/panoptes.example.toml", listed)
        self.assertNotIn("config/panoptes.toml", listed)
        self.assertNotIn(PRIVATE_LIST, listed)
        for name in PRODUCT_DIRS:
            self.assertTrue(any(rel.startswith(name + "/") for rel in listed), name)
        for name in ("README.md", "LICENSE", ".gitignore"):
            self.assertIn(name, listed)


class RunnerNamesTests(unittest.TestCase):
    """With no private list in the tree, the scan still knows its own runner."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_the_login_and_host_of_this_run_are_findings(self):
        root = tree(Path(self._tmp.name), {
            "docs/self.txt": f"notes by {getpass.getuser()} on {HOST}\n",
        })
        findings, _ = scan(root, root / "no-such-allow.txt")
        expected = {"docs/self.txt:1: username"}
        if release_scan.host_names():
            expected.add("docs/self.txt:1: hostname")
        self.assertEqual(set(findings), expected, findings)

    def test_a_missing_private_list_is_not_an_error(self):
        self.assertEqual(release_scan.private_names(Path(self._tmp.name)), [])


class AllowListTests(unittest.TestCase):
    """An allow entry excuses one named line and nothing else."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = tree(Path(self._tmp.name), {
            "wsd/planted.txt": "secret_token = Q7zLm3xK9pQ2\nnothing else here\n",
        })

    def allow(self, text: str) -> Path:
        path = self.root / "tools" / "release_scan_allow.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_allowlisted_line_passes(self):
        findings, _ = scan(self.root, self.allow(
            "wsd/planted.txt:1-token  # planted credential for the detector test\n"))
        self.assertEqual(findings, [])

    def test_allow_entry_needs_a_reason(self):
        findings, _ = scan(self.root, self.allow("wsd/planted.txt:1-token\n"))
        self.assertTrue(any("no reason" in line for line in findings), findings)
        # the excuse did not apply, so the planted secret is still reported
        self.assertIn("wsd/planted.txt:1: secret", findings)

    def test_allow_entry_with_an_empty_reason_is_rejected(self):
        entries = load_allow(self.allow("wsd/planted.txt:1-token  #   \n"))
        self.assertEqual(len(entries), 1)
        self.assertIn("no reason", entries[0].problem)

    def test_malformed_allow_entry_is_rejected(self):
        findings, _ = scan(self.root, self.allow("just some prose\n"))
        self.assertTrue(any("cannot parse" in line for line in findings), findings)

    def test_allow_entry_does_not_excuse_other_lines(self):
        tree(self.root, {"wsd/planted.txt": "a clean line\nsecret_token = Q7zLm3xK9pQ2\n"})
        findings, _ = scan(self.root, self.allow(
            "wsd/planted.txt:1-clean  # excuses the first line only\n"))
        self.assertIn("wsd/planted.txt:2: secret", findings)

    def test_allow_entry_that_matches_nothing_is_reported(self):
        findings, _ = scan(self.root, self.allow(
            "wsd/planted.txt:9-token  # a line that no longer exists\n"))
        self.assertIn("wsd/planted.txt:9: unused-allow", findings)


class ManifestScanTests(unittest.TestCase):
    """``.pcgsoft/`` is a product tree, so the portfolio manifest is scanned.

    The manifest is the file most likely to be written in a hurry with a real
    path or a private pointer in it, so a planted leak in a fake manifest must
    be reported exactly like one in ``wsd/``. The planted strings are built from
    constants so this file stays clean under the scan of the real repository.
    """

    MANIFEST = ".pcgsoft/project.yml"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = f"/home/{PLANT_LOGIN}/Projects/notes.txt"
        self.pointer = f"{PRIVATE_DIR}/receipts"

    def manifest_with(self, *lines: str) -> Path:
        body = "schemaVersion: 2\nproject:\n" + "".join(f"  {line}\n" for line in lines)
        return tree(Path(self._tmp.name), {self.MANIFEST: body})

    def test_a_planted_path_in_a_fake_manifest_is_caught(self):
        root = self.manifest_with(f'summary: notes in "{self.home}"',
                                  f"notes: see the {self.pointer} directory")
        findings, count = scan(root, root / "no-such-allow.txt")
        self.assertEqual(count, 1, findings)
        self.assertEqual(
            findings,
            # schemaVersion is line 1 and `project:` line 2, so the planted
            # lines land on 3 and 4 of the fake manifest
            [f"{self.MANIFEST}:3: home-path", f"{self.MANIFEST}:4: private-path"],
            findings,
        )

    def test_a_plain_manifest_is_clean(self):
        root = self.manifest_with("slug: widget", "summary: A local dashboard.")
        findings, _ = scan(root, root / "no-such-allow.txt")
        self.assertEqual(findings, [])

    def test_the_real_manifest_is_in_the_product_set(self):
        self.assertIn(self.MANIFEST, iter_files())


class RealRepoTests(unittest.TestCase):
    def test_the_product_set_scans_clean(self):
        findings, count = scan(REPO_ROOT)
        self.assertEqual(findings, [], "\n".join(findings))
        self.assertGreater(count, 100)


if __name__ == "__main__":
    unittest.main()
