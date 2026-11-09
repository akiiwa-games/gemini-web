"""Colab workstation checks use fake pages: no Chrome, no Google account, no network."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import colab_workstation as cw
import gemini_web

NOTEBOOK = "https://colab.research.google.com/drive/abc123"


class FakeLocator:
    def __init__(self, n):
        self.n = n
        self.first = self

    def count(self):
        return self.n

    def click(self, **_):
        pass


class FakeContext:
    def __init__(self):
        self.pages = []


class FakePage:
    def __init__(self, url=NOTEBOOK, texts=(), selectors=(), toolbar="Connect", connect_after=None):
        self.url = url
        self.texts = set(texts)
        self.selectors = set(selectors)
        self.toolbar = toolbar
        self.connect_after = connect_after  # toolbar text after Run all
        self.keys = []
        self.fronted = False
        self.context = FakeContext()
        self.context.pages.append(self)

    def get_by_text(self, text):
        return FakeLocator(int(text in self.texts))

    def locator(self, selector):
        return FakeLocator(int(selector in self.selectors))

    def evaluate(self, _script):
        return {"present": True, "text": self.toolbar, "tooltip": "", "compute_units_text": "93.5 compute units left"}

    @property
    def keyboard(self):
        page = self

        class Keyboard:
            def press(self, key):
                page.keys.append(key)
                if page.connect_after:
                    page.toolbar = page.connect_after

        return Keyboard()

    def bring_to_front(self):
        self.fronted = True

    def screenshot(self, path, **_):
        Path(path).write_bytes(b"png")


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class RunPageTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())

    def run_page(self, page, **kw):
        clock = FakeClock()
        return cw.run_page(page, "ultra_a", NOTEBOOK, self.out, sleep=clock.sleep, clock=clock, **kw)

    def test_signed_out_redirect_is_login_required_and_nothing_is_pressed(self):
        page = FakePage(url="https://accounts.google.com/ServiceLogin?continue=colab")
        state = self.run_page(page)
        self.assertEqual(state["status"], "login_required")
        self.assertEqual(page.keys, [])
        self.assertTrue(page.fronted)

    def test_colab_sign_in_link_is_login_required(self):
        state = self.run_page(FakePage(selectors={'a[href*="ServiceLogin"]'}))
        self.assertEqual((state["status"], state["reason"]), ("login_required", "colab_signed_out"))

    def test_security_challenge_and_captcha_stop_the_run(self):
        self.assertEqual(self.run_page(FakePage(texts={"Verify it's you"}))["reason"], "security_challenge")
        captcha = self.run_page(FakePage(texts={"I'm not a robot"}))
        self.assertEqual((captcha["status"], captcha["reason"]), ("consent_required", "captcha"))

    def test_drive_permission_dialog_after_run_all_is_consent_required(self):
        page = FakePage()
        original = page.keyboard.press

        def press_and_prompt(key):
            page.keys.append(key)
            page.texts.add("Permit this notebook to access your Google Drive files?")

        with mock.patch.object(FakePage, "keyboard", new_callable=mock.PropertyMock) as keyboard:
            keyboard.return_value = mock.Mock(press=press_and_prompt)
            state = self.run_page(page)
        self.assertIsNotNone(original)
        self.assertEqual((state["status"], state["reason"], state["stage"]),
                         ("consent_required", "drive_mount_consent", "after_run_all"))
        self.assertEqual(page.keys, ["Control+F9"])
        self.assertTrue(page.fronted)

    def test_oauth_popup_in_the_context_is_consent_required_and_brought_forward(self):
        page = FakePage()
        popup = FakePage(url="https://accounts.google.com/signin/oauth/consent?client_id=x")
        page.context.pages.append(popup)
        state = self.run_page(page)
        self.assertEqual((state["status"], state["reason"]), ("consent_required", "oauth_consent"))
        self.assertTrue(popup.fronted)
        self.assertEqual(page.keys, [])

    def test_prepare_only_inspects_runtime_without_running(self):
        page = FakePage(toolbar="T4 RAM Disk")
        state = self.run_page(page, prepare_only=True)
        self.assertEqual(state["status"], "prepared_in_browser")
        self.assertEqual(page.keys, [])
        self.assertTrue(state["runtime"]["connected"])
        self.assertEqual(state["runtime"]["gpu"], "T4")
        self.assertEqual(state["runtime"]["compute_units"], 93.5)

    def test_run_all_then_connected_runtime_is_launched(self):
        page = FakePage(connect_after="A100 RAM Disk")
        state = self.run_page(page)
        self.assertEqual(state["status"], "launched")
        self.assertEqual(page.keys, ["Control+F9"])
        self.assertTrue(state["runtime"]["connected"])
        self.assertEqual(state["runtime"]["gpu"], "A100")

    def test_non_colab_page_is_unexpected(self):
        self.assertEqual(self.run_page(FakePage(url="https://example.com/"))["status"], "unexpected_page")


class LaunchTests(unittest.TestCase):
    def test_launch_uses_the_accounts_bridge_and_writes_state(self):
        out = Path(tempfile.mkdtemp())
        page = FakePage(url="https://accounts.google.com/ServiceLogin")
        page.wait_for_timeout = lambda ms: None
        playwright = mock.MagicMock()
        with mock.patch.object(cw, "_sync_playwright", return_value=playwright), \
                mock.patch.object(gemini_web, "new_task_page", return_value=(object(), page)) as opened:
            state = cw.launch("ultra_b", NOTEBOOK, out)
        self.assertEqual(opened.call_args.kwargs["account"], "ultra_b")
        self.assertEqual(state["status"], "login_required")
        saved = json.loads((out / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "login_required")
        self.assertEqual(saved["account"], "ultra_b")

    def test_launch_rejects_non_https_urls(self):
        with self.assertRaises(ValueError):
            cw.launch("ultra_a", "javascript:alert(1)", Path(tempfile.mkdtemp()))


class HandoffTests(unittest.TestCase):
    def test_target_launches_first_and_ready_receipt_is_written(self):
        out = Path(tempfile.mkdtemp())
        calls = []

        def launcher(account, url, out_dir, **_):
            calls.append(account)
            return {"status": "launched", "account": account}

        receipt = cw.handoff("ultra_a", "ultra_b", NOTEBOOK, out, launcher=launcher)
        self.assertEqual(calls, ["ultra_b"])
        self.assertTrue(receipt["ready"])
        self.assertEqual(receipt["status"], "handoff_ready")
        self.assertTrue((out / "handoff.json").is_file())

    def test_blocked_target_keeps_the_source_serving(self):
        receipt = cw.handoff("ultra_a", "ultra_b", NOTEBOOK, Path(tempfile.mkdtemp()),
                             launcher=lambda *a, **k: {"status": "consent_required"})
        self.assertFalse(receipt["ready"])
        self.assertEqual(receipt["status"], "handoff_blocked:consent_required")
        self.assertIn("ultra_a keeps serving", receipt["next_step"])

    def test_registry_heartbeat_must_be_newer_than_the_handoff(self):
        registry = Path(tempfile.mkdtemp())
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        (registry / "ultra_b.json").write_text(json.dumps({"status": "running", "updated_at": stale}), encoding="utf-8")
        with mock.patch.object(cw, "wait_registry_ready", side_effect=lambda d, a, s, t: cw.read_registry(d, a)):
            receipt = cw.handoff("ultra_a", "ultra_b", NOTEBOOK, Path(tempfile.mkdtemp()), registry_dir=registry,
                                 launcher=lambda *a, **k: {"status": "launched"})
        self.assertFalse(receipt["ready"])
        fresh = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        self.assertTrue(cw.registry_fresh({"status": "running", "updated_at": fresh}, receipt["started_at"]))

    def test_same_account_is_rejected(self):
        with self.assertRaises(ValueError):
            cw.handoff("ultra_a", "ultra_a", NOTEBOOK, launcher=lambda *a, **k: {})


class BootstrapTests(unittest.TestCase):
    def test_notebook_is_valid_nbformat_with_mount_cache_and_registry(self):
        nb = cw.build_notebook('vllm serve "Qwen/Qwen3-8B" --port {port}', 8001, "cloudflared")
        json.loads(json.dumps(nb))
        self.assertEqual((nb["nbformat"], nb["nbformat_minor"]), (4, 5))
        self.assertEqual(nb["metadata"]["kernelspec"]["name"], "python3")
        for cell in nb["cells"]:
            self.assertIn(cell["cell_type"], ("markdown", "code"))
            self.assertIsInstance(cell["source"], list)
            if cell["cell_type"] == "code":
                self.assertEqual(cell["outputs"], [])
                compile("".join(cell["source"]), cell["metadata"]["id"], "exec")
        code = "".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
        for needle in ("drive.mount(mount)", "/content/drive/MyDrive/AI_Workstation", "/content/local_cache",
                       'os.environ["HF_HOME"]', "registry", "trycloudflare", '"gpu_name"', '"tunnel_url"',
                       '"started_at"', '"status"', "SERVER_PORT = 8001"):
            self.assertIn(needle, code)

    def test_bootstrap_helper_writes_registry_and_heartbeats(self):
        namespace = {}
        exec(compile(cw._render(cw.BOOTSTRAP_PY, "true", 8000, "none"), "bootstrap", "exec"), namespace)
        root = Path(tempfile.mkdtemp())
        (root / "registry").mkdir()
        path = namespace["write_registry"](str(root), "ultra_a", {"account": "ultra_a", "status": "starting"})
        self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8"))["status"], "starting")

        class Server:
            def poll(self):
                return 0  # exited: one beat then stop

        record = {"account": "ultra_a"}
        thread, _ = namespace["heartbeat"](str(root), "ultra_a", record, Server(), interval=0)
        thread.join(5)
        saved = json.loads((root / "registry" / "ultra_a.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "server_exited")
        self.assertIn("updated_at", saved)

    def test_init_bootstrap_writes_files_and_never_overwrites(self):
        out = Path(tempfile.mkdtemp())
        result = cw.init_bootstrap(out)
        self.assertEqual(result["files"], ["bootstrap_workstation.ipynb", "workstation_bootstrap.py",
                                           "workstation_bootstrap.sh"])
        json.loads((out / "bootstrap_workstation.ipynb").read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            cw.init_bootstrap(out)
        cw.init_bootstrap(out, overwrite=True)

    def test_unknown_tunnel_is_rejected(self):
        with self.assertRaises(ValueError):
            cw.build_notebook(tunnel="ngrok")


class StatusTests(unittest.TestCase):
    def test_status_reports_each_account_and_its_colab_tabs(self):
        def health(account=None):
            if account == "ultra_a":
                return {"pid": 7, "pages": [{"url": NOTEBOOK, "title": "nb"}, {"url": "https://drive.google.com/"}]}
            return None

        with mock.patch.object(gemini_web, "health", side_effect=health):
            report = cw.status()
        a, b = report["accounts"]
        self.assertEqual((a["account"], a["running"], len(a["colab_tabs"])), ("ultra_a", True, 1))
        self.assertEqual((b["account"], b["running"], b["cdp"]), ("ultra_b", False, "http://127.0.0.1:17852"))

    def test_accounts_can_be_configured(self):
        with mock.patch.dict("os.environ", {"COLAB_WORKSTATION_ACCOUNTS": "pro_x, pro-y"}):
            self.assertEqual(cw.configured_accounts(), ["pro_x", "pro-y"])

    def test_onboard_opens_drive_then_colab_in_the_named_profile(self):
        with mock.patch.object(gemini_web, "start", return_value="ok") as start, \
                mock.patch.object(gemini_web, "open_site", return_value={"id": "t"}) as opened:
            result = cw.onboard("ultra_a")
        start.assert_called_once_with(show=True, site="drive", account="ultra_a")
        opened.assert_called_once_with("colab", account="ultra_a")
        self.assertEqual(result["account"], "ultra_a")
        with self.assertRaises(gemini_web.BridgeError):
            cw.onboard("default")


if __name__ == "__main__":
    unittest.main()
