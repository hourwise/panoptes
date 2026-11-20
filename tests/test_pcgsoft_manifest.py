"""The PCGsoft portfolio manifest and the README markers it travels with (T-058).

Two files describe Panoptes to PCGsoft's portfolio sync: ``.pcgsoft/project.yml``
(a proposal, never publication authority — see ``docs/PROJECT_MANIFEST.md``) and
the two ``pcgsoft:`` marked sections at the top of ``README.md``. Both are read by
something that is not this repository, with rules this repository does not own, so
the rules are restated here as a table and checked offline.

The table below mirrors **PCGsoft's schema version 2** as documented in the site
repository's ``docs/portfolio-sync/project-manifest.md`` and implemented in its
``scripts/portfolio-reconcile.mjs`` (``V2_KEYS``, ``WEB_STATES_V2``,
``REPOSITORY_ROLES``, ``SLUG_PATTERN``, ``MAX_WEB_URLS``) and
``scripts/portfolio-sync-lib.mjs`` (``LIFECYCLE_STATES``, ``MAX_MANIFEST_BYTES``,
``MAX_TEXT_LENGTH``, ``MAX_README_SECTION_LENGTH`` and the safe YAML subset). If
the site changes, this table and the doc change together.

The last test hands the file to the site's own validator, offline, when a local
read-only clone of the site repository and ``node`` are both present; it skips
otherwise so a public checkout passes on its own. Nothing here touches the
network, and the site clone is only ever read.
"""

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / ".pcgsoft" / "project.yml"
README_PATH = ROOT / "README.md"

#: Allowed keys per block, schema version 2 (site: ``V2_KEYS``).
ALLOWED_KEYS = {
    "root": {"schemaVersion", "project", "repository", "web", "publication", "activity"},
    "project": {"slug", "name", "alternateNames", "category", "status", "statusLabel",
                "summary", "parentProject", "relatedProjects", "lastReviewed"},
    "repository": {"role"},
    "web": {"state", "urls"},
    "publication": {"eligible", "notes"},
    "activity": {"enabled"},
}

#: ``project.status`` (site: ``LIFECYCLE_STATES``).
LIFECYCLE_STATES = {"concept", "prototype", "active-development", "public-preview",
                    "live", "maintenance", "complete", "paused", "archived"}
#: ``web.state``, version 2 (site: ``WEB_STATES_V2``).
WEB_STATES = {"none", "preview", "live"}
#: ``repository.role`` (site: ``REPOSITORY_ROLES``).
REPOSITORY_ROLES = {"primary", "current", "original", "component", "supporting"}

#: Caps and shapes (site: ``MAX_MANIFEST_BYTES``, ``MAX_TEXT_LENGTH``,
#: ``MAX_README_SECTION_LENGTH``, ``MAX_WEB_URLS``, ``SLUG_PATTERN``).
MAX_MANIFEST_BYTES = 24_000
MAX_TEXT_LENGTH = 2_000
MAX_README_SECTION = 4_000
MAX_WEB_URLS = 10
#: The summary is a portfolio blurb, not a paragraph: self-imposed, tighter than
#: the site's 2,000-character cap, and the agreed length for this project.
MAX_SUMMARY = 300

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: YAML constructs the site's parser refuses: anchors, aliases, tags, directives,
#: explicit keys and block scalars. A value or key starting with one of these is
#: a parse failure there, so it is one here.
UNSAFE_YAML_RE = re.compile(r"(?:^|[\s:\[])([&*!%?|>@][^\s]*)")

#: Things a portfolio description must never contain, however innocent the line
#: looks in a private checkout.
URL_RE = re.compile(r"https?://|//\S*\.[a-z]{2,}", re.I)
ABSOLUTE_PATH_RE = re.compile(r"(?:^|\s)/\S|~/|[A-Za-z]:\\")
HOSTNAME_RE = re.compile(r"\b[\w-]+\.(?:com|org|net|edu|gov|io|dev|app|local|internal|lan)\b", re.I)
IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


class ManifestFormatError(ValueError):
    """The file is not in the safe YAML subset the site parser accepts."""


def scalar(raw: str, where: int):
    """One scalar: null/booleans/integers, a quoted string, an inline list, or a bare string."""
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [] if not inner else [scalar(part.strip(), where) for part in inner.split(",")]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        body = raw[1:-1]
        if raw[0] == '"':
            try:
                return json.loads(raw)
            except json.JSONDecodeError as error:
                raise ManifestFormatError(f"line {where}: bad double-quoted string ({error})") from error
        return body.replace("''", "'")
    if raw == "null" or raw == "~":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if re.fullmatch(r"-?(?:0|[1-9]\d*)", raw):
        return int(raw)
    return raw


def parse_safe_yaml(text: str) -> dict:
    """Parse the safe manifest subset: mappings, lists of scalars, comments.

    Deliberately stricter than general YAML and deliberately independent of the
    site's parser: tabs, anchors, aliases, tags, directives, block scalars,
    mappings inside lists and deeper nesting are all rejected rather than
    guessed at, because the site's parser rejects them.
    """
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise ManifestFormatError(f"line {number}: tabs are not indentation")
        content = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not content or content == "---":
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        unsafe = UNSAFE_YAML_RE.search(content)
        if unsafe:
            raise ManifestFormatError(f"line {number}: {unsafe.group(1)!r} is not safe YAML")
        lines.append((indent, content, number))
    if not lines:
        raise ManifestFormatError("the manifest is empty")
    if lines[0][0] != 0:
        raise ManifestFormatError(f"line {lines[0][2]}: the root mapping is indented")
    value, index = _block(lines, 0, 0, 0)
    if index != len(lines):
        raise ManifestFormatError(f"line {lines[index][2]}: unexpected indentation")
    if not isinstance(value, dict):
        raise ManifestFormatError("the root of the manifest must be a mapping")
    return value


def _block(lines, start, indent, depth):
    """One mapping or list at ``indent``; returns (value, index of the next line)."""
    if depth > 2:
        raise ManifestFormatError(f"line {lines[start][2]}: nested deeper than the schema allows")
    if lines[start][1].startswith("- "):
        items = []
        index = start
        while index < len(lines) and lines[index][0] == indent:
            _, content, number = lines[index]
            if not content.startswith("- "):
                raise ManifestFormatError(f"line {number}: mixed mapping and list values")
            item = content[2:].strip()
            if not item:
                raise ManifestFormatError(f"line {number}: empty list item")
            if ": " in item or item.endswith(":"):
                raise ManifestFormatError(f"line {number}: a list may hold scalars only")
            items.append(scalar(item, number))
            index += 1
        return items, index
    result = {}
    index = start
    while index < len(lines):
        line_indent, content, number = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ManifestFormatError(f"line {number}: unexpected indentation")
        if content.startswith("- "):
            raise ManifestFormatError(f"line {number}: a list is not valid here")
        key, separator, raw_value = content.partition(":")
        key = key.strip()
        if not separator or not key:
            raise ManifestFormatError(f"line {number}: expected a key and a colon")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            raise ManifestFormatError(f"line {number}: unsafe key {key!r}")
        if key in result:
            raise ManifestFormatError(f"line {number}: duplicate key {key!r}")
        raw_value = raw_value.strip()
        if raw_value:
            result[key] = scalar(raw_value, number)
            index += 1
        elif index + 1 < len(lines) and lines[index + 1][0] > indent:
            result[key], index = _block(lines, index + 1, lines[index + 1][0], depth + 1)
        else:
            result[key] = None
            index += 1
    return result, index


def read_manifest() -> dict:
    """The repository's manifest, parsed; a failure here is a test failure."""
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    return parse_safe_yaml(text)


def readme_section(name: str) -> tuple[str, int, int]:
    """The text between one pair of markers, and how often each marker appears."""
    text = README_PATH.read_text(encoding="utf-8")
    starts = re.findall(rf"<!--\s*pcgsoft:{name}:start\s*-->", text)
    ends = re.findall(rf"<!--\s*pcgsoft:{name}:end\s*-->", text)
    match = re.search(rf"<!--\s*pcgsoft:{name}:start\s*-->([\s\S]*?)<!--\s*pcgsoft:{name}:end\s*-->", text)
    return (match.group(1).strip() if match else ""), len(starts), len(ends)


class ManifestFileTests(unittest.TestCase):
    def test_the_manifest_exists_and_is_within_the_size_cap(self):
        self.assertTrue(MANIFEST_PATH.is_file(), ".pcgsoft/project.yml is missing")
        size = MANIFEST_PATH.stat().st_size
        self.assertLessEqual(size, MAX_MANIFEST_BYTES,
                             f"the site refuses a manifest over {MAX_MANIFEST_BYTES} bytes")

    def test_the_manifest_parses_in_the_safe_subset(self):
        try:
            parsed = read_manifest()
        except ManifestFormatError as error:
            self.fail(f"not in the safe YAML subset: {error}")
        self.assertIsInstance(parsed, dict)

    def test_only_the_allowed_version_two_keys_are_used(self):
        parsed = read_manifest()
        self.assertEqual(sorted(set(parsed) - ALLOWED_KEYS["root"]), [],
                         "unknown key at the root of the manifest")
        for block, allowed in ALLOWED_KEYS.items():
            if block == "root":
                continue
            value = parsed.get(block)
            if value is None:
                continue
            self.assertIsInstance(value, dict, f"{block} must be a mapping")
            self.assertEqual(sorted(set(value) - allowed), [], f"unknown key under {block}")


class ManifestValueTests(unittest.TestCase):
    def setUp(self):
        self.manifest = read_manifest()
        self.project = self.manifest.get("project") or {}

    def test_the_required_fields_and_the_enums(self):
        self.assertEqual(self.manifest.get("schemaVersion"), 2)
        self.assertRegex(str(self.project.get("slug")), SLUG_RE.pattern)
        for field in ("name", "category", "statusLabel", "summary"):
            value = self.project.get(field)
            self.assertIsInstance(value, str, f"project.{field} must be a string")
            self.assertTrue(value.strip(), f"project.{field} must not be empty")
            self.assertLessEqual(len(value), MAX_TEXT_LENGTH, f"project.{field} is too long")
        self.assertIn(self.project.get("status"), LIFECYCLE_STATES)
        self.assertRegex(str(self.project.get("lastReviewed")), DATE_RE.pattern)
        role = (self.manifest.get("repository") or {}).get("role")
        self.assertIn(role, REPOSITORY_ROLES)
        web = self.manifest.get("web") or {}
        self.assertIn(web.get("state"), WEB_STATES)
        urls = web.get("urls")
        self.assertIsInstance(urls, list, "web.urls must be a list")
        self.assertLessEqual(len(urls), MAX_WEB_URLS)
        for url in urls:
            self.assertTrue(str(url).startswith("https://"), f"{url} must be HTTPS")
        for field, value in self.project.items():
            if isinstance(value, str):
                self.assertLessEqual(len(value), MAX_TEXT_LENGTH, f"project.{field} is too long")

    def test_the_values_this_project_agreed_to(self):
        self.assertEqual(self.project.get("slug"), "panoptes")
        self.assertEqual(self.project.get("name"), "Panoptes")
        self.assertEqual(self.project.get("category"), "open-source")
        self.assertEqual(self.project.get("status"), "active-development")
        self.assertIs(self.project.get("parentProject"), None)
        self.assertEqual(self.project.get("relatedProjects"), [])
        self.assertEqual((self.manifest.get("repository") or {}).get("role"), "primary")
        self.assertEqual((self.manifest.get("web") or {}).get("state"), "none")
        self.assertEqual((self.manifest.get("web") or {}).get("urls"), [],
                         "Panoptes has no hosted site, so it may not propose URLs")
        publication = self.manifest.get("publication") or {}
        self.assertIs(publication.get("eligible"), False,
                      "publication is PCGsoft's decision, never this file's")
        self.assertTrue(str(publication.get("notes") or "").strip())
        self.assertIs((self.manifest.get("activity") or {}).get("enabled"), False)

    def test_the_summary_is_short_and_says_nothing_about_this_machine(self):
        summary = self.project.get("summary") or ""
        self.assertTrue(summary.strip(), "the summary must not be empty")
        self.assertLessEqual(len(summary), MAX_SUMMARY)
        self.assertNotIn("/", summary, "a summary is prose, not a path or a URL")
        for pattern, label in ((URL_RE, "a URL"), (ABSOLUTE_PATH_RE, "a path"),
                               (HOSTNAME_RE, "a hostname"), (IPV4_RE, "an IP address")):
            found = pattern.search(summary)
            self.assertIsNone(found, f"the summary contains {label}: {found and found.group(0)}")

    def test_no_value_anywhere_in_the_manifest_leaks_this_machine(self):
        offenders = []
        for block, values in self.manifest.items():
            items = values.items() if isinstance(values, dict) else [(block, values)]
            for key, value in items:
                if not isinstance(value, str):
                    continue
                for pattern, label in ((URL_RE, "URL"), (HOSTNAME_RE, "hostname"),
                                       (IPV4_RE, "IP address"),
                                       (re.compile(r"/home/|~/|[A-Za-z]:\\"), "path")):
                    if pattern.search(value):
                        offenders.append(f"{block}.{key}: {label}")
        self.assertEqual(offenders, [])


class ReadmeMarkerTests(unittest.TestCase):
    MARKERS = ("overview", "status-notes")

    def test_each_marker_pair_appears_once_and_is_balanced(self):
        for name in self.MARKERS:
            with self.subTest(marker=name):
                body, starts, ends = readme_section(name)
                self.assertEqual(starts, 1, f"pcgsoft:{name}:start must appear exactly once")
                self.assertEqual(ends, 1, f"pcgsoft:{name}:end must appear exactly once")
                self.assertTrue(body, f"the pcgsoft:{name} section is empty")

    def test_each_section_is_within_the_length_cap(self):
        for name in self.MARKERS:
            with self.subTest(marker=name):
                body, _, _ = readme_section(name)
                self.assertLessEqual(len(body), MAX_README_SECTION,
                                     f"the site drops a pcgsoft:{name} section over "
                                     f"{MAX_README_SECTION} characters")

    def test_the_sections_carry_no_machine_specific_value(self):
        for name in self.MARKERS:
            body, _, _ = readme_section(name)
            with self.subTest(marker=name):
                for pattern, label in ((URL_RE, "a URL"), (HOSTNAME_RE, "a hostname"),
                                       (IPV4_RE, "an IP address"),
                                       (re.compile(r"/home/|~/|[A-Za-z]:\\"), "a local path"),
                                       (re.compile(r"localhost|\b\d+\.\d+\.\d+\.\d+"), "a host")):
                    found = pattern.search(body)
                    self.assertIsNone(found, f"pcgsoft:{name} contains {label}: {found and found.group(0)}")


def site_clone() -> Path | None:
    """A local read-only clone of the PCGsoft site repository, or None.

    ``PCGSOFT_SITE`` overrides the usual place; nothing is downloaded and nothing
    in the clone is ever written.
    """
    override = os.environ.get("PCGSOFT_SITE")
    base = Path(override).expanduser() if override else Path.home() / "Projects" / "PCGsoft" / "panoptes-integration"
    needed = ("scripts/portfolio-sync-lib.mjs", "scripts/portfolio-reconcile.mjs")
    return base if all((base / part).is_file() for part in needed) else None


class SiteValidatorTests(unittest.TestCase):
    """Validate the manifest with PCGsoft's own code, offline and read-only."""

    def setUp(self):
        self.clone = site_clone()
        if self.clone is None:
            self.skipTest("no local clone of the PCGsoft site repository "
                          "(set PCGSOFT_SITE to one, with scripts/portfolio-sync-lib.mjs)")
        self.node = shutil.which("node")
        if self.node is None:
            self.skipTest("node is not on PATH")

    def _script(self) -> str:
        lib = (self.clone / "scripts" / "portfolio-sync-lib.mjs").as_uri()
        reconcile = (self.clone / "scripts" / "portfolio-reconcile.mjs").as_uri()
        return (
            f"const s=await import({lib!r});const r=await import({reconcile!r});"
            f"const fs=await import('node:fs');const t=fs.readFileSync({str(MANIFEST_PATH)!r},'utf8');"
            "const errors=[];let v1Rejection=null;"
            "try{r.validateManifestV2(s.parseSafeYaml(t));}catch(e){errors.push('validateManifestV2: '+e.message);}"
            "try{s.validateManifest(s.parseSafeYaml(t));}catch(e){v1Rejection=e.message;}"
            "const c=await r.parseManifestContent(t);"
            "errors.push(...c.errors.map(e=>'parseManifestContent: '+e));"
            "console.log(JSON.stringify({schemaVersion:c.schemaVersion,"
            "declaredProject:c.declaredProject,errors,v1Rejection}));"
        )

    def test_the_site_validator_accepts_the_manifest(self):
        run = subprocess.run([self.node, "--input-type=module", "-e", self._script()],
                             cwd=self.clone, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr.strip())
        report = json.loads(run.stdout)
        self.assertEqual(report["errors"], [], run.stdout.strip())
        self.assertEqual(report["schemaVersion"], 2)
        self.assertEqual(report["declaredProject"], "panoptes")
        # validateManifest is the site's version-1 validator, so it must refuse a
        # version-2 file: the site parses what it recognises and fails closed on
        # the rest. parseManifestContent, the path the sync itself uses, is the
        # one that has to accept this file.
        rejection = report["v1Rejection"]
        if rejection is not None:
            self.assertTrue("must be 1" in rejection or "not an allowed field" in rejection,
                            f"unexpected version-1 rejection: {rejection}")


if __name__ == "__main__":
    unittest.main()
