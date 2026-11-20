#!/usr/bin/env python3
"""Release scan: prove the shipped files say nothing about this machine.

The product is published as a clean export of these files, so nothing in them
may identify the workstation they were written on: no home directory of a real
user, no host name, no e-mail address, no credential, no hardware serial, no
private project name — unless the line says it is an example. Run it before
publishing anything; exit 0 means the tree is clean, exit 1 lists what is not.

    python3 tools/release_scan.py

Detectors (per file, per line, reported as ``file:line: kind``):

``home-path``   ``/home/<user>/`` where the user is not one of the placeholders
                ``YOUR_USER``, ``user``, ``alice``, ``bob``.
``hostname``    this machine's ``socket.gethostname()`` and any ``*.local`` name.
``email``       an e-mail address; ``@localhost``, ``@example.com`` and
                ``@example.org`` are allowed.
``secret``      a ``token``/``secret``/``password``/``api_key``-style assignment
                whose value looks like a literal credential rather than a
                program expression. The name part reuses ``wsd.reports._SECRET``,
                the pattern that redacts downloaded reports, so both agree about
                what counts as a secret assignment.
``serial``      a ``serial``-shaped key with a long hex/alnum value.
``private-name`` a name listed in the local private-name list (see below).
``username``    the login name of whoever runs the scan, as a whole word, in any
                case: prose that names the author is not product text.
``private-path`` a path into the private project-management tree (``PRIVATE_DIR``,
                written with a trailing slash) inside a file that ships.

The names that belong to one particular machine never appear in this file, so
that the scanner itself can be published. They come from
``config/release_scan_private.txt``: one name per line, ``#`` starting a comment.
That file lives in a private checkout only — the public export ships one file
from ``config/`` and it is not this one — and the scan never reads it as input
text. It is optional: with no list the scan still adds the login name of whoever
runs it and this machine's host name, so a public checkout catches its own
developer's name and host for free.

``config/panoptes.toml`` is this machine's live configuration: it stays in the
private repository and is never part of the product set. ``tools/
release_scan_allow.txt`` lists the few intended exceptions, one
``file:line-substring  # reason`` per line; an entry without a reason is itself
a failure, and so is an entry that no longer matches anything.
"""

from __future__ import annotations

import functools
import getpass
import re
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wsd.reports import _SECRET  # noqa: E402  (the report redactor's pattern)

#: Trees and single files that make up the published product.
PRODUCT_DIRS = ("wsd", "web", "tests", "tools", "ops", "docs", "config", ".pcgsoft")
PRODUCT_FILES = ("README.md", "LICENSE", ".gitignore", "pyproject.toml")

#: The local list of machine-specific names (see the module docstring). Private
#: material: never published, never scanned as product text.
PRIVATE_LIST = "config/release_scan_private.txt"

#: Product-set exceptions: this machine's live configuration and its private
#: name list, neither of which is published.
EXCLUDED = {"config/panoptes.toml", PRIVATE_LIST}

SKIP_DIRS = {"__pycache__", ".git", "node_modules"}

#: Home-directory owners that are placeholders rather than people.
ALLOWED_HOME_USERS = ("YOUR_USER", "user", "alice", "bob")

#: The private project-management tree (task packets, receipts, design drafts)
#: that never ships: a product file must not point into it.
PRIVATE_DIR = ".pm"

#: E-mail domains that say nothing about who or where the machine is.
ALLOWED_EMAIL_DOMAINS = ("localhost", "example.com", "example.org")

_HOME = re.compile(r"(?<![\w.~+\-])/home/([A-Za-z0-9._+-]+)")
_LAN_NAME = re.compile(r"(?<![\w.\-])(?:[\w\-]+\.)*[\w\-]+\.local\b")
_EMAIL = re.compile(r"(?<![\w.%+\-])(?P<local>[\w.%+\-]+)@(?P<domain>[\w\-]+(?:\.[\w\-]+)+)")
_SERIAL = re.compile(
    r"(?<![\w\-])[\"']?[A-Za-z0-9_-]*serial[A-Za-z0-9_-]*[\"']?[\"']?\s*[:=]\s*[\"']?[0-9A-Za-z]{12,}"
)
# A literal credential, as opposed to a program expression: no code punctuation,
# no dotted module path, and either a digit in it (sk-abc123, hunter2) or long
# enough that prose and identifiers would not produce it by accident.
_SECRET_VALUE = re.compile(r"[A-Za-z0-9_@:/+=#+\-]{6,}")
_PRIVATE_PATH_RE = re.compile(rf"(?<!\w){re.escape(PRIVATE_DIR)}/")

#: ``file:lineno-substring  # reason``
_ALLOW_ENTRY = re.compile(r"^(?P<file>[^:\s]+):(?P<line>[0-9]+)-(?P<substr>.*?)\s+#\s*(?P<reason>\S.*)$")
_ALLOW_SHAPE = re.compile(r"^[^:\s]+:[0-9]+-.+")


class Finding:
    """One problem on one line of one file: what to show is ``file:line: kind``."""

    __slots__ = ("path", "line", "kind")

    def __init__(self, path: str, line: int, kind: str):
        self.path, self.line, self.kind = path, line, kind

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


class AllowEntry:
    """One permitted exception, with the reason that justifies it."""

    __slots__ = ("path", "line", "substr", "reason", "problem")

    def __init__(self, path="", line=0, substr="", reason="", problem=""):
        self.path, self.line, self.substr, self.reason, self.problem = path, line, substr, reason, problem


def host_names() -> list[str]:
    """This machine's names: the host name and its short form, longest first."""
    try:
        name = socket.gethostname() or ""
    except OSError:
        return []
    names = [candidate for candidate in dict.fromkeys((name, name.split(".")[0])) if len(candidate) >= 3]
    return sorted(names, key=len, reverse=True)


#: One private name as a whole word: a name inside a longer word or a hyphenated
#: run is a different string, not a finding.
@functools.lru_cache(maxsize=None)
def _word_rx(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w\-]){re.escape(name)}(?![\w\-])", re.IGNORECASE)


def private_names(root: Path = ROOT) -> list[str]:
    """The local private-name list: one name per line, ``#`` starts a comment.

    This file exists only in a private checkout; a missing list is not an error,
    because the runner's own login and host names are always added on top.
    """
    path = Path(root) / PRIVATE_LIST
    if not path.is_file():
        return []
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        name = raw.split("#", 1)[0].strip()
        if name:
            names.append(name)
    return names


def login_name() -> str:
    """Whoever runs the scan: that name says who wrote the files."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return ""


def kinds_in_line(line: str, hosts: list[str] | None = None,
                  private: list[str] | None = None, user: str | None = None) -> list[str]:
    """Every detector that fires on one line of text."""
    kinds: list[str] = []
    if any(user not in ALLOWED_HOME_USERS for user in _HOME.findall(line)):
        kinds.append("home-path")
    names = host_names() if hosts is None else hosts
    if _LAN_NAME.search(line) or any(h.lower() in line.lower() for h in names if h):
        kinds.append("hostname")
    if any(not _email_allowed(m["domain"]) for m in _EMAIL.finditer(line)):
        kinds.append("email")
    if any(_looks_secret(m.group(4)) for m in _SECRET.finditer(line)):
        kinds.append("secret")
    if _SERIAL.search(line):
        kinds.append("serial")
    listed = private_names() if private is None else private
    if any(_word_rx(name).search(line) for name in listed if name):
        kinds.append("private-name")
    runner = login_name() if user is None else user
    if runner and _word_rx(runner).search(line):
        kinds.append("username")
    if _PRIVATE_PATH_RE.search(line):
        kinds.append("private-path")
    return kinds


def _email_allowed(domain: str) -> bool:
    domain = domain.lower().strip(".")
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def _looks_secret(value: str) -> bool:
    """A credential-shaped literal value, not a variable, call or JSON boolean."""
    value = value.strip("\"'")
    if not _SECRET_VALUE.fullmatch(value):
        return False
    return any(c.isdigit() for c in value) or len(value) >= 16


def iter_files(root: Path = ROOT) -> list[str]:
    """The product file set, as repo-relative paths, sorted."""
    found: list[str] = []
    for name in PRODUCT_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or set(path.relative_to(base).parts) & SKIP_DIRS:
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in EXCLUDED:
                found.append(rel)
    for name in PRODUCT_FILES:
        if (root / name).is_file():
            found.append(name)
    return sorted(found)


def read_text(rel: str, root: Path = ROOT) -> str | None:
    """File text, or None when it is binary or unreadable."""
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def load_allow(path: Path) -> list[AllowEntry]:
    """Parse the allow list; a bad entry comes back with ``problem`` set."""
    if not path.is_file():
        return []
    entries: list[AllowEntry] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ALLOW_ENTRY.match(line)
        if match:
            entries.append(AllowEntry(match["file"], int(match["line"]), match["substr"],
                                      match["reason"].strip()))
        elif _ALLOW_SHAPE.match(line):
            entries.append(AllowEntry(line=0, problem=f"{path.name}: {line} has no reason"))
        else:
            entries.append(AllowEntry(line=0, problem=f"{path.name}: cannot parse {line}"))
    return entries


def scan(root: Path = ROOT, allow_path: Path | None = None) -> tuple[list[str], int]:
    """Scan a product tree; returns (sorted ``file:line: kind`` lines, files read).

    Never echoes a matched value: only the kind of problem is reported.
    """
    root = Path(root)
    allow_path = root / "tools" / "release_scan_allow.txt" if allow_path is None else Path(allow_path)
    allow = load_allow(allow_path)
    entries = [e for e in allow if not e.problem]
    findings: list[str] = [e.problem for e in allow if e.problem]
    used = set()
    hosts = host_names()
    listed = private_names(root)
    runner = login_name()
    files = iter_files(root)
    for rel in files:
        text = read_text(rel, root)
        if text is None:
            continue
        lines = text.splitlines()
        applicable = [e for e in entries if e.path == rel]
        for lineno, line in enumerate(lines, 1):
            kinds = kinds_in_line(line, hosts, listed, runner)
            if not kinds:
                continue
            for entry in applicable:
                if entry.line == lineno and entry.substr in line:
                    used.add(id(entry))
                    kinds = []
                    break
            for kind in kinds:
                findings.append(f"{rel}:{lineno}: {kind}")
    for entry in entries:
        if id(entry) not in used:
            findings.append(f"{entry.path}:{entry.line}: unused-allow")
    return sorted(findings), len(files)


def main(argv: list[str] = sys.argv[1:]) -> int:
    findings, count = scan()
    for line in findings:
        print(line)
    if findings:
        print(f"NOT CLEAN: {count} files scanned, {len(findings)} problem(s)")
        return 1
    print(f"CLEAN: {count} files scanned, 0 problems")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
