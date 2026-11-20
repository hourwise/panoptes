"""Live CDP round trip against real headless Chrome via tools/cdp.py.

Guards the regression where `WebSocket._read_exactly` received bytes but
never buffered them, so every command reply was swallowed and timed out.
Skipped when /usr/bin/google-chrome is missing.
"""

import os
import unittest

from tools.cdp import CHROME, Chrome

skip = unittest.skipUnless(os.path.exists(CHROME), f"{CHROME} is not installed")


@skip
class LiveCdpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chrome = Chrome().start()
        cls.chrome.cdp.command_timeout = 5.0

    @classmethod
    def tearDownClass(cls):
        cls.chrome.stop()

    def test_create_target_navigate_and_evaluate(self):
        page = self.chrome.open_page()  # Target.createTarget + attach + enables
        self.assertTrue(page.target_id)
        self.assertTrue(page.session_id)
        self.chrome.navigate(page, "about:blank")
        self.assertEqual(self.chrome.evaluate(page, "1+1"), 2)


if __name__ == "__main__":
    unittest.main()
