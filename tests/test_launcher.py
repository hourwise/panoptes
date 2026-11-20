"""Tests for the Linux desktop launcher files in ops/desktop (T-032).

The real launcher is never executed and nothing is written outside a temp dir.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DESKTOP = REPO / "ops" / "desktop"
LAUNCH = DESKTOP / "panoptes-launch.sh"
INSTALL = DESKTOP / "install-launcher.sh"
TEMPLATE = DESKTOP / "panoptes.desktop.in"
SVG = DESKTOP / "panoptes.svg"

REQUIRED_KEYS = [
    "Type=Application",
    "Name=Panoptes",
    "Comment=AI workstation control centre",
    "Exec=@LAUNCH@",
    "Icon=@ICON@",
    "Terminal=false",
    "Categories=System;Monitor;",
    "StartupNotify=true",
]


class ScriptSyntaxTest(unittest.TestCase):
    def test_bash_syntax(self):
        for script in (LAUNCH, INSTALL):
            with self.subTest(script=script.name):
                r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_scripts_are_executable(self):
        for script in (LAUNCH, INSTALL):
            with self.subTest(script=script.name):
                self.assertTrue(os.access(script, os.X_OK))

    def test_launcher_has_no_hardcoded_home_path(self):
        text = LAUNCH.read_text()
        self.assertNotIn("/home", text)


class TemplateTest(unittest.TestCase):
    def test_template_contains_required_keys(self):
        text = TEMPLATE.read_text()
        for key in REQUIRED_KEYS:
            with self.subTest(key=key):
                self.assertIn(key + "\n", text)

    def test_template_has_placeholders(self):
        text = TEMPLATE.read_text()
        self.assertIn("@LAUNCH@", text)
        self.assertIn("@ICON@", text)

    def test_icon_svg_valid(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(SVG.read_text())
        self.assertEqual(root.get("viewBox"), "0 0 256 256")


class InstallerTest(unittest.TestCase):
    def _run(self, args, env_extra):
        env = dict(os.environ)
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(INSTALL), *args], capture_output=True, text=True, env=env
        )

    def _tmp_env(self, tmp):
        return {"XDG_DATA_HOME": str(tmp), "HOME": str(tmp)}

    def test_install_then_uninstall(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            dest = tmp / "applications" / "panoptes.desktop"
            r = self._run([], self._tmp_env(tmp))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(dest.is_file(), "installer did not create the .desktop file")
            text = dest.read_text()

            self.assertNotIn("@LAUNCH@", text)
            self.assertNotIn("@ICON@", text)
            exec_line = [l for l in text.splitlines() if l.startswith("Exec=")]
            icon_line = [l for l in text.splitlines() if l.startswith("Icon=")]
            self.assertEqual(len(exec_line), 1)
            self.assertEqual(len(icon_line), 1)
            exec_val = exec_line[0][len("Exec="):]
            icon_val = icon_line[0][len("Icon="):]
            self.assertTrue(os.path.isabs(exec_val), exec_val)
            self.assertTrue(os.path.isabs(icon_val), icon_val)
            self.assertEqual(Path(exec_val).resolve(), LAUNCH.resolve())
            self.assertEqual(Path(icon_val).resolve(), SVG.resolve())
            for key in REQUIRED_KEYS:
                self.assertIn(key.replace("@LAUNCH@", exec_val).replace("@ICON@", icon_val), text)
            if shutil.which("desktop-file-validate"):
                v = subprocess.run(["desktop-file-validate", str(dest)], capture_output=True, text=True)
                self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
                self.assertEqual((v.stdout + v.stderr).strip(), "", "validator reported issues")

            r = self._run(["--uninstall"], self._tmp_env(tmp))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(dest.exists(), "--uninstall left the file behind")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uninstall_is_idempotent(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            r = self._run(["--uninstall"], self._tmp_env(tmp))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bad_argument_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            r = self._run(["--frobnicate"], self._tmp_env(tmp))
            self.assertNotEqual(r.returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
