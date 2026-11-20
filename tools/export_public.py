#!/usr/bin/env python3
"""Build the clean public export of Panoptes.

    python3 tools/export_public.py DEST

``DEST`` must not exist yet and must not be inside this repository: the export is
a fresh tree a stranger receives, not a copy of the development checkout. It
holds the product set only — ``wsd/``, ``web/``, ``tests/``, ``tools/``,
``ops/``, ``docs/``, ``.pcgsoft/``, ``config/panoptes.example.toml``,
``README.md``, ``LICENSE`` and ``.gitignore`` — and never the private material:
project-management tree, the git and assistant directories, this machine's live
``config/panoptes.toml``, the local ``config/release_scan_private.txt`` name
list, byte-code and test caches, and no symbolic link.

``DEST/MANIFEST.sha256`` is written last over the copied files: one
``<sha256>  <relative path>`` line per file, sorted, so a receiver can check the
tree byte for byte.

Finally the export is scanned with ``tools/release_scan.py`` over ``DEST``.
A finding means the export is not publishable: the exit code is 1 and ``DEST``
is left in place for inspection. This tool runs no version-control command,
pushes nothing and opens no socket — publishing stays a human decision
(see ``docs/RELEASE.md``).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import release_scan  # noqa: E402

#: Trees copied whole (less what ``SKIP_NAMES`` and ``NEVER`` rule out).
PRODUCT_DIRS = ("wsd", "web", "tests", "tools", "ops", "docs", ".pcgsoft")

#: Loose files that ship as they are.
PRODUCT_FILES = ("README.md", "LICENSE", ".gitignore")

#: Paths copied one by one, because their tree does not ship as a whole.
PRODUCT_PATHS = ("config/panoptes.example.toml",)

#: Directory or file names that never ship, wherever they appear.
SKIP_NAMES = {".pm", ".git", ".claude", ".pytest_cache", "__pycache__", "node_modules"}

#: Exact product-set paths that stay private even if their tree is exported:
#: this machine's live configuration and the local list of machine-specific
#: names the release scan looks for.
NEVER = {"config/panoptes.toml", release_scan.PRIVATE_LIST}

#: What the manifest is called, and where it goes.
MANIFEST = "MANIFEST.sha256"


def _skipped(name: str) -> bool:
    """A name that never ships: a private or cached directory, or byte code."""
    return name in SKIP_NAMES or name.endswith(".pyc")


def sources(root: Path = ROOT) -> list[str]:
    """Every file the export ships, as sorted export-relative paths.

    Symlinks are never listed: neither a linked file nor a linked directory is
    ever followed or copied, so the export cannot reach outside the repository.
    """
    picked: list[str] = []
    for rel in (*PRODUCT_FILES, *PRODUCT_PATHS):
        source = root / rel
        if source.is_file() and not source.is_symlink() and rel not in NEVER:
            picked.append(rel)
    for name in PRODUCT_DIRS:
        base = root / name
        if not base.is_dir() or base.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            dirnames[:] = [d for d in sorted(dirnames)
                           if not _skipped(d) and not (here / d).is_symlink()]
            for filename in sorted(filenames):
                source = here / filename
                rel = source.relative_to(root).as_posix()
                if _skipped(filename) or rel in NEVER or source.is_symlink():
                    continue
                picked.append(rel)
    return sorted(set(picked))


def sha256_of(path: Path) -> str:
    """The file's sha256, read in blocks so a large file costs no memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def dest_problem(dest: Path, root: Path = ROOT) -> str | None:
    """Why ``dest`` cannot be written, or None when it can."""
    target = Path(dest)
    if target.exists() or target.is_symlink():
        return f"{target} already exists; the export writes a new directory"
    resolved = target.resolve()
    repo = Path(root).resolve()
    if resolved == repo or repo in resolved.parents:
        return f"{target} is inside this repository; the export goes outside it"
    return None


def build(dest: Path, root: Path = ROOT) -> list[str]:
    """Copy the product set into a new ``dest`` and write its manifest.

    Returns the exported relative paths, sorted. Nothing outside ``dest`` is
    written, and no file is written before the destination was checked.
    """
    target = Path(dest)
    target.mkdir(parents=True)
    copied: list[str] = []
    for rel in sources(root):
        source = root / rel
        outlet = target / rel
        outlet.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, outlet)
        copied.append(rel)
    lines = [f"{sha256_of(target / rel)}  {rel}" for rel in copied]
    (target / MANIFEST).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return copied


def main(argv: list[str], root: Path = ROOT) -> int:
    """The command line: 0 exported and clean, 1 exported but not clean, 2 refused."""
    if len(argv) != 1:
        print("usage: python3 tools/export_public.py DEST", file=sys.stderr)
        return 2
    dest = Path(argv[0])
    problem = dest_problem(dest, root)
    if problem:
        print(f"refused: {problem}", file=sys.stderr)
        return 2
    copied = build(dest, root)
    print(f"exported {len(copied)} files to {dest}")
    print(f"manifest: {dest / MANIFEST}")
    findings, scanned = release_scan.scan(dest)
    for line in findings:
        print(line)
    if findings:
        print(f"NOT CLEAN: {scanned} files scanned, {len(findings)} problem(s); "
              f"{dest} left for inspection")
        return 1
    print(f"CLEAN: {scanned} files scanned, 0 problems")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
