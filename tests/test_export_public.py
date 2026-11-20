"""The clean public export (T-049): what it copies, what it refuses, what it writes.

Each test builds a small fake repository in a temp directory and runs
``tools/export_public.py`` over it, so nothing here reads or writes the real
repository, and every path the export could touch is inside the temp dir.
"""

import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from tools import export_public

#: A home directory the scan's home-path detector does not excuse, built from a
#: name so this file stays clean while the exported copy of it is not.
LEAK_USER = "morgan"
LEAK_PATH = f"/home/{LEAK_USER}/notes.txt"

#: The private project-management tree, named through this constant so that this
#: file itself stays clean under the scan the export runs.
PRIVATE = ".pm"

#: The PCGsoft portfolio manifest: proposal data, but product text that ships.
PCGSOFT_MANIFEST = ".pcgsoft/project.yml"

#: A repository shaped like the product, plus everything the export must leave out.
FILES = {
    "wsd/server.py": "print('serving')\n",
    "wsd/__pycache__/server.cpython-312.pyc": "byte code\n",
    "web/index.html": "<html></html>\n",
    "tests/test_server.py": "import unittest\n",
    "tests/fixtures/sample.txt": "fixture\n",
    "tools/smoke.py": "print('smoke')\n",
    "tools/release_scan.py": "print('scan')\n",
    "tools/release_scan_allow.txt": "# nothing is excused here\n",
    "ops/desktop/install-launcher.sh": "#!/bin/bash\n",
    "docs/INSTALL.md": "# Installing\n",
    "config/panoptes.example.toml": "# commented example\n",
    "config/release_scan_private.txt": "the name of a project on this machine\n",
    "config/panoptes.toml": "base_url = 'http://127.0.0.1:8000'\n",
    "README.md": "# Panoptes\n",
    "LICENSE": "MIT\n",
    ".gitignore": "__pycache__/\n",
    PRIVATE + "/tasks/T-001.md": "a private task packet\n",
    PRIVATE + "/receipts/T-001.md": "a private receipt\n",
    ".git/config": "[core]\n",
    ".claude/settings.json": "{}\n",
    ".pytest_cache/lastfailed": "{}\n",
    "notes/scratch.md": "not part of the product\n",
    "AGENTS.md": "rules for the implementation worker\n",
    PCGSOFT_MANIFEST: "schemaVersion: 2\n",
}

#: Everything of ``FILES`` that is product: the export's complete file list.
EXPECTED = [
    ".gitignore",
    PCGSOFT_MANIFEST,
    "LICENSE",
    "README.md",
    "config/panoptes.example.toml",
    "docs/INSTALL.md",
    "ops/desktop/install-launcher.sh",
    "tests/fixtures/sample.txt",
    "tests/test_server.py",
    "tools/release_scan.py",
    "tools/release_scan_allow.txt",
    "tools/smoke.py",
    "web/index.html",
    "wsd/server.py",
]


class ExportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo = self.base / "repo"
        for rel, text in FILES.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        # links inside the product trees, a linked directory, and a link out of
        # the repository: none of them may be copied or followed
        os.symlink("server.py", self.repo / "wsd" / "linked.py")
        os.symlink("../notes", self.repo / "wsd" / "linked_dir")
        os.symlink(str(self.base), self.repo / "tests" / "link_out")
        self.dest = self.base / "export"

    def run_export(self, *argv: str) -> tuple[int, str]:
        """``main`` on these arguments, with everything it printed."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = export_public.main(list(argv), root=self.repo)
        return code, out.getvalue()


class CopyTest(ExportCase):
    def test_only_the_product_set_is_exported(self):
        copied = export_public.build(self.dest, self.repo)
        self.assertEqual(copied, EXPECTED)
        on_disk = sorted(path.relative_to(self.dest).as_posix()
                         for path in self.dest.rglob("*") if path.is_file())
        self.assertEqual(on_disk, sorted([*EXPECTED, export_public.MANIFEST]))

    def test_the_pcgsoft_manifest_is_exported(self):
        copied = export_public.build(self.dest, self.repo)
        self.assertIn(PCGSOFT_MANIFEST, copied)
        self.assertEqual(
            (self.dest / PCGSOFT_MANIFEST).read_text(encoding="utf-8"),
            FILES[PCGSOFT_MANIFEST],
        )

    def test_private_cached_and_linked_material_is_excluded(self):
        export_public.build(self.dest, self.repo)
        for rel in (PRIVATE, ".git", ".claude", ".pytest_cache", "notes", "AGENTS.md",
                    "config/panoptes.toml", "config/release_scan_private.txt",
                    "wsd/__pycache__", "wsd/linked.py", "wsd/linked_dir", "tests/link_out"):
            self.assertFalse((self.dest / rel).exists(), rel)
        self.assertEqual(list(self.dest.rglob("*.pyc")), [])
        self.assertEqual([path for path in self.dest.rglob("*") if path.is_symlink()], [])

    def test_the_manifest_lists_every_file_with_its_hash(self):
        export_public.build(self.dest, self.repo)
        lines = (self.dest / export_public.MANIFEST).read_text(encoding="utf-8").splitlines()
        paths = [line.split("  ", 1)[1] for line in lines]
        self.assertEqual(paths, EXPECTED)
        self.assertEqual(paths, sorted(paths))
        for line in lines:
            digest, rel = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((self.dest / rel).read_bytes()).hexdigest(), rel)


class RefusalTest(ExportCase):
    def test_an_existing_dest_is_refused_and_untouched(self):
        self.dest.mkdir()
        (self.dest / "keep.txt").write_text("mine\n", encoding="utf-8")
        code, out = self.run_export(str(self.dest))
        self.assertEqual(code, 2)
        self.assertIn("refused", out)
        self.assertEqual([path.name for path in self.dest.iterdir()], ["keep.txt"])

    def test_a_dest_inside_the_repository_is_refused(self):
        for rel in ("export", "wsd/nested/export"):
            with self.subTest(dest=rel):
                target = self.repo / rel
                code, out = self.run_export(str(target))
                self.assertEqual(code, 2, rel)
                self.assertIn("inside this repository", out)
                self.assertFalse(target.exists(), rel)

    def test_the_wrong_number_of_arguments_is_refused(self):
        for argv in ([], [str(self.dest), str(self.base / "second")]):
            with self.subTest(argv=argv):
                code, out = self.run_export(*argv)
                self.assertEqual(code, 2)
                self.assertIn("usage", out)


class ScanTest(ExportCase):
    def test_a_clean_export_exits_zero_and_says_so(self):
        code, out = self.run_export(str(self.dest))
        self.assertEqual(code, 0, out)
        self.assertIn(f"exported {len(EXPECTED)} files", out)
        self.assertIn("CLEAN", out)
        self.assertTrue((self.dest / export_public.MANIFEST).is_file())

    def test_a_planted_scan_finding_fails_the_export_and_keeps_dest(self):
        (self.repo / "wsd" / "leak.py").write_text(f"LOG = {LEAK_PATH!r}\n", encoding="utf-8")
        code, out = self.run_export(str(self.dest))
        self.assertEqual(code, 1, out)
        self.assertIn("wsd/leak.py:1: home-path", out)
        self.assertIn("NOT CLEAN", out)
        # left in place, exactly as exported, so the finding can be inspected
        self.assertEqual((self.dest / "wsd" / "leak.py").read_text(encoding="utf-8"),
                         f"LOG = {LEAK_PATH!r}\n")

    def test_the_export_module_never_shells_out_or_opens_sockets(self):
        for name in ("subprocess", "socket", "urllib", "http"):
            self.assertFalse(hasattr(export_public, name), name)


if __name__ == "__main__":
    unittest.main()
