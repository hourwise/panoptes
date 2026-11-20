"""wsd.profiles: listing, diffing and reading an engine's profiles — read-only."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from wsd import profiles

ACTIVE = "flashnext.yaml"
OTHER = "flashnext-spec6.yaml"


class ProfileDir(unittest.TestCase):
    """A profiles directory with two differing profiles plus noise to ignore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write(ACTIVE, "model: flashnext\nctx: 8192\nthreads: 8\n")
        self.write(OTHER, "model: flashnext\nctx: 4096\nthreads: 8\n")

    def write(self, name: str, text: str, where: Path | None = None) -> Path:
        path = (where or self.root) / name
        path.write_text(text)
        return path

    def names(self, active: str | None = ACTIVE) -> list[str]:
        return [row["name"] for row in profiles.list_profiles(self.root, active)]

    def row(self, name: str) -> dict:
        return next(row for row in profiles.list_profiles(self.root, ACTIVE) if row["name"] == name)


class ListTest(ProfileDir):
    def test_rows_carry_the_documented_fields(self):
        row = self.row(OTHER)
        self.assertEqual(
            sorted(row),
            ["active", "diff_vs_active", "modified", "name", "sha256_12", "size"],
        )
        self.assertEqual(row["size"], (self.root / OTHER).stat().st_size)

    def test_active_first_then_by_name(self):
        self.write("aaa-first.yaml", "ctx: 1\n")
        self.write("zzz-last.yaml", "ctx: 2\n")
        self.assertEqual(
            self.names(),
            [ACTIVE, "aaa-first.yaml", OTHER, "zzz-last.yaml"],
        )

    def test_active_flag_only_on_the_active_file(self):
        self.assertEqual([row["active"] for row in profiles.list_profiles(self.root, ACTIVE)], [True, False])

    def test_sha_is_the_first_12_hex_of_the_content_hash(self):
        data = (self.root / OTHER).read_bytes()
        self.assertEqual(self.row(OTHER)["sha256_12"], hashlib.sha256(data).hexdigest()[:12])
        self.assertRegex(self.row(OTHER)["sha256_12"], r"^[0-9a-f]{12}$")

    def test_modified_is_an_iso_utc_timestamp(self):
        self.assertRegex(self.row(OTHER)["modified"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

    def test_diff_shows_the_change_against_the_active_profile(self):
        diff = self.row(OTHER)["diff_vs_active"]
        self.assertIn(f"--- {ACTIVE}", diff)
        self.assertIn(f"+++ {OTHER}", diff)
        self.assertIn("-ctx: 8192", diff)
        self.assertIn("+ctx: 4096", diff)

    def test_active_profile_has_an_empty_diff(self):
        self.assertEqual(self.row(ACTIVE)["diff_vs_active"], "")

    def test_no_active_profile_means_no_diffs_at_all(self):
        self.assertEqual(self.names(active=None), [OTHER, ACTIVE])
        for row in profiles.list_profiles(self.root, None):
            self.assertFalse(row["active"])
            self.assertEqual(row["diff_vs_active"], "")

    def test_identical_files_diff_to_nothing(self):
        self.write("flashnext-copy.yaml", "model: flashnext\nctx: 8192\nthreads: 8\n")
        self.assertEqual(self.row("flashnext-copy.yaml")["diff_vs_active"], "")

    def test_diff_is_cut_at_the_limit_with_a_marker(self):
        self.write(ACTIVE, "\n".join(f"line {n}" for n in range(400)))
        self.write(OTHER, "\n".join(f"line {n}x" for n in range(400)))
        lines = self.row(OTHER)["diff_vs_active"].splitlines()
        self.assertEqual(len(lines), profiles.MAX_DIFF_LINES)
        self.assertTrue(lines[-1].endswith("more diff line(s) truncated"))

    def test_only_yaml_files_directly_in_the_directory(self):
        (self.root / "notes.txt").write_text("read me\n")
        (self.root / "profile.json").write_text("{}\n")
        (self.root / ".hidden.yaml").write_text("sneaky\n")
        (self.root / "sub").mkdir()
        self.write("nested.yaml", "ctx: 1\n", where=self.root / "sub")
        self.assertEqual(self.names(), [ACTIVE, OTHER])

    def test_yml_extension_counts_as_a_profile(self):
        self.write("legacy.yml", "ctx: 2\n")
        self.assertEqual(self.names(), [ACTIVE, OTHER, "legacy.yml"])

    def test_missing_directory_raises_instead_of_inventing_rows(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.list_profiles(self.root / "gone")
        with self.assertRaises(profiles.ProfileError):
            profiles.list_profiles(self.root / ACTIVE)  # a file is not a directory

    def test_a_directory_of_nothing_lists_nothing(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(profiles.list_profiles(empty), [])

    def test_large_file_is_read_to_the_cap_but_sized_as_it_lies(self):
        big = self.write("big.yaml", "x" * (profiles.MAX_BYTES + 4096))
        row = next(row for row in profiles.list_profiles(self.root) if row["name"] == "big.yaml")
        self.assertEqual(row["size"], big.stat().st_size)
        self.assertEqual(len(profiles.read_profile(self.root, "big.yaml")), profiles.MAX_BYTES)


class ReadTest(ProfileDir):
    def test_returns_the_file_text(self):
        self.assertEqual(profiles.read_profile(self.root, OTHER), "model: flashnext\nctx: 4096\nthreads: 8\n")

    def test_missing_file_raises(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.read_profile(self.root, "flashnext-spec7.yaml")

    def test_traversal_names_are_rejected(self):
        for name in ("../evil.yaml", "..", "", "a/b.yaml", "/etc/passwd", "flashnext.yaml/../../etc/passwd"):
            with self.subTest(name=name):
                with self.assertRaises(profiles.ProfileError):
                    profiles.read_profile(self.root, name)

    def test_names_that_are_not_profiles_are_rejected(self):
        for name in ("notes.txt", ".hidden.yaml", "flashnext.YAML", "flash next.yaml", "flashnext.yaml\x00"):
            with self.subTest(name=name):
                with self.assertRaises(profiles.ProfileError):
                    profiles.read_profile(self.root, name)

    def test_undecodable_bytes_are_replaced_not_fatal(self):
        (self.root / "binary.yaml").write_bytes(b"ctx: 8192\n\xff\xfe\n")
        self.assertIn("\ufffd", profiles.read_profile(self.root, "binary.yaml"))

    def test_a_symlink_out_of_the_directory_is_never_served(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        secret = self.write("secret.yaml", "token: hunter2\n", where=Path(outside.name))
        try:
            os.symlink(secret, self.root / "escape.yaml")
        except OSError as exc:  # a filesystem that refuses symlinks
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertNotIn("escape.yaml", self.names())
        with self.assertRaises(profiles.ProfileError):
            profiles.read_profile(self.root, "escape.yaml")
        served = "\n".join(row["diff_vs_active"] for row in profiles.list_profiles(self.root, ACTIVE))
        self.assertNotIn("hunter2", served)

    def test_a_symlink_within_the_directory_is_served(self):
        os.symlink(self.root / OTHER, self.root / "alias.yaml")
        self.assertIn("alias.yaml", self.names())
        self.assertEqual(profiles.read_profile(self.root, "alias.yaml"), profiles.read_profile(self.root, OTHER))

    def test_reading_never_touches_the_files(self):
        def state():
            return sorted((path.name, path.stat().st_mtime_ns, path.read_bytes()) for path in self.root.iterdir())

        before = state()
        profiles.list_profiles(self.root, ACTIVE)
        profiles.read_profile(self.root, OTHER)
        self.assertEqual(before, state())


if __name__ == "__main__":
    unittest.main()
