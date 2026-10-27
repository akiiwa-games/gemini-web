import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gemini_web


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(gemini_web, "PROFILE", self.tmp / "chrome-profile"),
            mock.patch.object(gemini_web, "DOWNLOADS", self.tmp / "downloads"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def preferences(self):
        return self.tmp / "chrome-profile" / "Default" / "Preferences"

    def test_downloads_never_prompt_and_land_in_the_bridge_folder(self):
        gemini_web._prepare_profile()
        data = json.loads(self.preferences().read_text(encoding="utf-8"))
        self.assertFalse(data["download"]["prompt_for_download"])
        self.assertEqual(data["download"]["default_directory"], str(self.tmp / "downloads"))
        self.assertTrue((self.tmp / "downloads").is_dir())

    def test_existing_preferences_are_kept(self):
        self.preferences().parent.mkdir(parents=True)
        self.preferences().write_text(json.dumps({"profile": {"name": "x"}, "download": {"extra": 1}}), encoding="utf-8")
        gemini_web._prepare_profile()
        data = json.loads(self.preferences().read_text(encoding="utf-8"))
        self.assertEqual(data["profile"], {"name": "x"})
        self.assertEqual(data["download"]["extra"], 1)

    def test_unreadable_preferences_are_not_overwritten(self):
        self.preferences().parent.mkdir(parents=True)
        self.preferences().write_text("{not json", encoding="utf-8")
        gemini_web._prepare_profile()
        self.assertEqual(self.preferences().read_text(encoding="utf-8"), "{not json")


class ProcessTests(unittest.TestCase):
    def test_only_the_bridge_profile_counts_as_ours(self):
        ours = gemini_web.chrome_args(["https://gemini.google.com/app"])
        self.assertTrue(gemini_web._is_ours(ours))
        self.assertFalse(gemini_web._is_ours([str(gemini_web.CHROME), "--profile-directory=Default"]))
        self.assertFalse(gemini_web._is_ours([str(gemini_web.CHROME), r"--user-data-dir=C:\other"]))

    def test_launch_flags_keep_cdp_on_loopback_without_automation_markers(self):
        args = gemini_web.chrome_args([])
        self.assertIn(f"--remote-debugging-port={gemini_web.CDP_PORT}", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        # --enable-automation sets navigator.webdriver, which Google sign-in refuses.
        self.assertFalse(any(a.startswith("--enable-automation") for a in args))

    def test_health_rejects_a_port_owned_by_another_process(self):
        with mock.patch.object(gemini_web, "_is_our_browser_process", side_effect=lambda pid: pid == 10), \
                mock.patch.object(gemini_web, "cdp_version", return_value={"Browser": "Chrome/1"}), \
                mock.patch.object(gemini_web, "_port_owner", return_value=99):
            self.assertIsNone(gemini_web.health())

    def test_main_pid_trusts_the_port_owner_only_when_it_runs_our_profile(self):
        with mock.patch.object(gemini_web, "_port_owner", return_value=10), \
                mock.patch.object(gemini_web, "_is_our_browser_process", side_effect=lambda pid: pid == 10), \
                mock.patch.object(gemini_web, "_chrome_pids") as listed:
            self.assertEqual(gemini_web.main_pid(), 10)
            listed.assert_not_called()
        with mock.patch.object(gemini_web, "_port_owner", return_value=99), \
                mock.patch.object(gemini_web, "_is_our_browser_process", side_effect=lambda pid: pid == 12), \
                mock.patch.object(gemini_web, "_chrome_pids", return_value=[11, 12]):
            self.assertEqual(gemini_web.main_pid(), 12)


if __name__ == "__main__":
    unittest.main()
