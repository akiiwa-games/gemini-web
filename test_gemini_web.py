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


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(gemini_web, "DATA_DIR", self.tmp / "data"),
            mock.patch.object(gemini_web, "HOME", self.tmp / "home"),
            mock.patch.dict(gemini_web.os.environ, {}, clear=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for key in [k for k in gemini_web.os.environ if k.startswith("GEMINI_WEB_CDP_PORT_")]:
            del gemini_web.os.environ[key]

    def test_default_account_is_the_historical_single_profile(self):
        for name in (None, "default", ""):
            cfg = gemini_web.account_config(name)
            self.assertTrue(cfg.is_default)
            self.assertEqual(cfg.profile, gemini_web.PROFILE)
            self.assertEqual(cfg.downloads, gemini_web.DOWNLOADS)
            self.assertEqual(cfg.descriptor, gemini_web.DESCRIPTOR)
            self.assertEqual(cfg.port, gemini_web.CDP_PORT)
        self.assertEqual(gemini_web.chrome_args(["u"]), gemini_web.chrome_args(["u"], account="default"))
        self.assertIn("--window-name=Gemini Web", gemini_web.chrome_args([]))

    def test_named_accounts_get_isolated_profiles_runtime_and_ports(self):
        a, b = gemini_web.account_config("ultra_a"), gemini_web.account_config("ultra_b")
        self.assertEqual(a.profile, self.tmp / "data" / "accounts" / "ultra_a" / "chrome-profile")
        self.assertEqual(a.descriptor, self.tmp / "home" / "accounts" / "ultra_a" / "runtime" / "browser.json")
        self.assertEqual(b.downloads, self.tmp / "home" / "accounts" / "ultra_b" / "downloads")
        self.assertEqual((a.port, b.port), (17850, 17852))
        ports = {gemini_web.CDP_PORT, a.port, b.port}
        self.assertEqual(len(ports), 3)
        self.assertNotIn(17851, (a.port, b.port))  # AgentCompass account-login Chrome
        args = gemini_web.chrome_args([], account="ultra_a")
        self.assertIn(f"--user-data-dir={a.profile}", args)
        self.assertIn("--remote-debugging-port=17850", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertTrue(gemini_web._is_ours(args, a.profile))
        self.assertFalse(gemini_web._is_ours(args))  # never mistaken for the default bridge
        self.assertFalse(gemini_web._is_ours(args, b.profile))

    def test_custom_accounts_get_stable_free_ports_and_overrides(self):
        first = gemini_web.account_port("lab-1")
        self.assertEqual(first, gemini_web.account_port("lab-1"))
        self.assertGreaterEqual(first, gemini_web.HASHED_PORT_BASE)
        self.assertNotIn(first, gemini_web.RESERVED_PORTS | {17850, 17852})
        (self.tmp / "home").mkdir()
        (self.tmp / "home" / "accounts.json").write_text(json.dumps({"accounts": {"lab-1": {"port": 18001}}}),
                                                         encoding="utf-8")
        self.assertEqual(gemini_web.account_port("lab-1"), 18001)
        gemini_web.os.environ["GEMINI_WEB_CDP_PORT_LAB_1"] = "18002"
        self.assertEqual(gemini_web.account_port("lab-1"), 18002)

    def test_invalid_account_names_are_rejected(self):
        for bad in ("../x", "a b", "x" * 65, "ü"):
            with self.assertRaises(gemini_web.BridgeError):
                gemini_web.account_config(bad)

    def test_named_health_checks_its_own_port_and_profile(self):
        cfg = gemini_web.account_config("ultra_a")
        with mock.patch.object(gemini_web, "_port_owner", return_value=5) as owner, \
                mock.patch.object(gemini_web, "_is_our_browser_process", return_value=True) as ours, \
                mock.patch.object(gemini_web, "cdp_version", return_value={"Browser": "Chrome/1"}) as version, \
                mock.patch.object(gemini_web, "_get_json", return_value=[]):
            live = gemini_web.health(account="ultra_a")
        owner.assert_called_once_with(17850)
        ours.assert_called_once_with(5, cfg.profile)
        version.assert_called_once_with("http://127.0.0.1:17850")
        self.assertEqual((live["account"], live["cdp"]), ("ultra_a", "http://127.0.0.1:17850"))

    def test_new_task_page_attaches_to_the_accounts_endpoint(self):
        playwright = mock.MagicMock()
        context = playwright.chromium.connect_over_cdp.return_value.contexts[0]
        with mock.patch.object(gemini_web, "start") as start:
            gemini_web.new_task_page(playwright, "https://colab.research.google.com/", account="ultra_b")
        start.assert_called_once_with(show=False, account="ultra_b")
        playwright.chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:17852")
        context.new_page.return_value.goto.assert_called_once()

    def test_background_page_opens_unactivated_tab_and_minimizes_a_cold_bridge(self):
        playwright = mock.MagicMock()
        browser = playwright.chromium.connect_over_cdp.return_value
        cdp = browser.new_browser_cdp_session.return_value
        cdp.send.side_effect = lambda method, params=None: {
            "Target.getTargets": {"targetInfos": [{"type": "page", "targetId": "t1"},
                                                  {"type": "service_worker", "targetId": "t2"}]},
            "Browser.getWindowForTarget": {"windowId": 7}}.get(method, {})
        with mock.patch.object(gemini_web, "start"), mock.patch.object(gemini_web, "health", return_value=None):
            gemini_web.new_background_page(playwright, "https://accounts.google.com/x", account="ultra_b")
        calls = [c.args for c in cdp.send.call_args_list]
        self.assertIn(("Browser.setWindowBounds", {"windowId": 7, "bounds": {"windowState": "minimized"}}), calls)
        self.assertIn(("Target.createTarget", {"url": "about:blank", "background": True}), calls)
        browser.contexts[0].new_page.assert_not_called()

    def test_named_profile_preferences_use_the_accounts_downloads(self):
        gemini_web._prepare_profile(account="ultra_a")
        cfg = gemini_web.account_config("ultra_a")
        data = json.loads((cfg.profile / "Default" / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(data["download"]["default_directory"], str(cfg.downloads))

    def test_cli_strips_account_flag(self):
        self.assertEqual(gemini_web._pop_account(["show", "colab", "--account", "ultra_a"]),
                         (["show", "colab"], "ultra_a"))
        self.assertEqual(gemini_web._pop_account(["stop", "--account=ultra_b"]), (["stop"], "ultra_b"))
        with mock.patch.object(gemini_web, "start", return_value="ok") as start:
            self.assertEqual(gemini_web.main(["show", "drive", "--account", "ultra_a"]), 0)
        start.assert_called_once_with(show=True, site="drive", account="ultra_a")
        with mock.patch.object(gemini_web, "start", return_value="ok") as start:
            gemini_web.main(["start", "--show"])
        start.assert_called_once_with(show=True, site="gemini")  # default call shape unchanged


class SiteTests(unittest.TestCase):
    def test_colab_and_drive_sites(self):
        self.assertEqual(gemini_web.SITES["colab"], "https://colab.research.google.com/")
        self.assertEqual(gemini_web.SITES["drive"], "https://drive.google.com/drive/my-drive")
        for key in ("gemini", "aistudio", "flow"):
            self.assertIn(key, gemini_web.SITES)

    def test_open_site_resolves_colab_on_the_accounts_endpoint(self):
        with mock.patch.object(gemini_web, "_get_json", return_value={"id": "t1"}) as get_json, \
                mock.patch.object(gemini_web.urllib.request, "urlopen") as urlopen:
            result = gemini_web.open_site("colab", account="ultra_a")
        self.assertEqual(result["url"], "https://colab.research.google.com/")
        self.assertEqual(get_json.call_args.kwargs["endpoint"], "http://127.0.0.1:17850")
        self.assertIn("127.0.0.1:17850/json/activate/t1", urlopen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
