"""Documentation gate (T-033): README and docs/ must stay true to the repo.

Checks the shipped documentation for the mistakes that creep in as the code
moves under it:

- every doc file exists and is non-empty (README and each guide in `docs/`);
- every repository path mentioned in a doc (``ops/...``, ``tools/...`` or
  ``wsd/...``) exists — no references to files that were never added;
- every module named in a ``python3 -m <module>`` command is importable;
- no ``/home/`` path appears outside a line that marks it as an example, so
  machine-specific values are always labelled;
- no serial-number-like hex string (12+ hex characters in one token) — docs
  must not smuggle in identifiers, hashes or licence keys.

Pure text checks over files in the repo; no server, no network.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOC_FILES = ("README.md", "docs/INSTALL.md", "docs/ROLLBACK.md", "docs/SECURITY.md",
             "docs/RELEASE.md", "docs/PROJECT_MANIFEST.md")

#: repo-relative paths of the shipped code/docs trees mentioned in prose
PATH_RE = re.compile(r"\b(?:ops|tools|wsd)/[\w./-]+")

#: `python3 -m <module>` in any command form
MODULE_RE = re.compile(r"python3 -m ([\w.]+)")

#: one token of 12+ hex digits = serial/id/hash-shaped
HEX_RE = re.compile(r"\b[0-9a-fA-F]{12,}\b")


def _doc_lines():
    for name in DOC_FILES:
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            yield name, lineno, line


def _clean_path(token: str) -> str:
    """Strip sentence punctuation a regex may have pulled in behind a path."""
    return token.rstrip(".,;:")


class DocFileTests(unittest.TestCase):
    def test_doc_files_exist_and_are_non_empty(self):
        for name in DOC_FILES:
            path = ROOT / name
            self.assertTrue(path.is_file(), f"missing doc file: {name}")
            self.assertGreater(
                len(path.read_text(encoding="utf-8").strip()),
                200,
                f"doc file is empty or a stub: {name}",
            )


class DocPathTests(unittest.TestCase):
    def test_mentioned_repo_paths_exist(self):
        missing = []
        for name, lineno, line in _doc_lines():
            for token in PATH_RE.findall(line):
                rel = _clean_path(token).rstrip("/")
                if not (ROOT / rel).exists():
                    missing.append(f"{name}:{lineno}: {token}")
        self.assertEqual(missing, [], "docs reference non-existent repo paths")

    def test_mentioned_python_modules_are_importable(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        broken = []
        for name, lineno, line in _doc_lines():
            for module in MODULE_RE.findall(line):
                try:
                    found = importlib.util.find_spec(module) is not None
                except (ImportError, ModuleNotFoundError, ValueError):
                    found = False
                if not found:
                    broken.append(f"{name}:{lineno}: python3 -m {module}")
        self.assertEqual(broken, [], "docs name modules that do not exist")


class DocHygieneTests(unittest.TestCase):
    def test_home_paths_are_always_marked_as_examples(self):
        offenders = [
            f"{name}:{lineno}"
            for name, lineno, line in _doc_lines()
            if "/home/" in line and "example" not in line.lower()
        ]
        self.assertEqual(
            offenders, [], "lines with /home/ paths must say they are an example"
        )

    def test_no_serial_like_hex_strings(self):
        offenders = [
            f"{name}:{lineno}: {HEX_RE.search(line).group(0)}"
            for name, lineno, line in _doc_lines()
            if HEX_RE.search(line)
        ]
        self.assertEqual(offenders, [], "docs contain serial-number-like hex strings")


class RollbackContentTests(unittest.TestCase):
    def test_rollback_mentions_post_backup_file_removal(self):
        text = (ROOT / "docs/ROLLBACK.md").read_text(encoding="utf-8")
        self.assertIn(
            "models.yml",
            text,
            "ROLLBACK.md must tell the reader to remove agent/models.yml "
            "(created after the OMP backup) before restoring the tarball",
        )


class VersionTests(unittest.TestCase):
    """The README states the version the package states (RELEASE.md section 5)."""

    def test_readme_states_the_package_version(self):
        from wsd import __version__
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            f"Version {__version__}.",
            readme,
            "the README version line must match wsd.__version__",
        )


if __name__ == "__main__":
    unittest.main()
