"""Colab workstation checks: fake pages and a fake colab-cli; no Chrome, no Google account, no network."""
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import colab_workstation as cw
import gemini_web


class FakeLocator:
    def __init__(self, page, key, visible):
        self.page, self.key, self.visible = page, key, visible
        self.first = self.last = self

    def count(self):
        return int(self.visible)

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return True

    def is_checked(self):
        return self.key in self.page.checked

    def check(self):
        self.page.checked.add(self.key)
        self.page.actions.append(("check", self.key))

    def click(self, **_):
        self.page.actions.append(("click", self.key))
        self.page.on_click(self.key)

    def scroll_into_view_if_needed(self, **_):
        pass


class FakePage:
    """A Google consent page: role/name lookups answer from `buttons`, `checkboxes`, `texts`, `selectors`."""

    def __init__(self, url, buttons=(), checkboxes=(), texts=(), selectors=(), after_click=None):
        self.url = url
        self.buttons, self.checkboxes = set(buttons), set(checkboxes)
        self.texts, self.selectors = set(texts), set(selectors)
        self.checked, self.actions = set(), []
        self.after_click = after_click or {}
        self.fronted = 0

    def get_by_text(self, text):
        return FakeLocator(self, text, text in self.texts)

    def locator(self, selector):
        return FakeLocator(self, selector, selector in self.selectors)

    def get_by_role(self, role, name):
        pool = self.buttons if role == "button" else self.checkboxes
        hit = next((label for label in pool if name.search(label)), None)
        return FakeLocator(self, hit, hit is not None)

    def on_click(self, key):
        if key in self.after_click:
            self.url = self.after_click[key]

    def bring_to_front(self):
        self.fronted += 1


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


DONE = "https://colab.research.google.com/tun/m/authorize-for-drive-credentials-ephem?ok"


def consent(page, auto=True, timeout=30):
    clock = Clock()
    return cw.drive_consent(page, lambda u: cw.DRIVE_REDIRECT_MARKER in u, email="a@example.com",
                            auto_consent=auto, timeout=timeout, sleep=clock.sleep, clock=clock)


class ConsentTests(unittest.TestCase):
    def test_granular_screen_selects_all_then_continues(self):
        page = FakePage("https://accounts.google.com/signin/oauth/v3/consent", buttons={"Continue", "Cancel"},
                        checkboxes={"Select all"}, after_click={"Continue": DONE})
        result = consent(page)
        self.assertEqual(result["status"], "granted")
        self.assertEqual(page.actions, [("check", "Select all"), ("click", "Continue")])
        self.assertEqual(page.fronted, 0)

    def test_already_granted_screen_only_continues(self):
        page = FakePage("https://accounts.google.com/signin/oauth/id", buttons={"Continue", "Cancel"},
                        after_click={"Continue": DONE})
        self.assertEqual(consent(page)["clicks"], ["continue"])

    def test_without_auto_consent_nothing_is_clicked_and_window_surfaces(self):
        page = FakePage("https://accounts.google.com/signin/oauth/id", buttons={"Continue"},
                        checkboxes={"Select all"})
        result = consent(page, auto=False)
        self.assertEqual(result["status"], "consent_required")
        self.assertEqual(page.actions, [])
        self.assertEqual(page.fronted, 1)

    def test_password_page_is_left_to_the_owner(self):
        page = FakePage("https://accounts.google.com/v3/signin/identifier", buttons={"Continue"},
                        selectors={'input[type="password"]'})
        result = consent(page)
        self.assertEqual(result["status"], "sign_in")
        self.assertEqual(page.actions, [])
        self.assertEqual(page.fronted, 1)

    def test_challenge_and_captcha_are_never_clicked(self):
        for url, texts, reason in (("https://accounts.google.com/signin/challenge/pwd", (), "security_challenge"),
                                   ("https://accounts.google.com/x", ("I'm not a robot",), "captcha")):
            page = FakePage(url, buttons={"Continue"}, texts=texts)
            self.assertEqual(consent(page)["status"], reason)
            self.assertEqual(page.actions, [])

    def test_owner_finishing_in_window_counts_as_granted(self):
        page = FakePage("https://accounts.google.com/signin/oauth/id", buttons={"Continue"})
        clock = Clock()

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.now > 5:
                page.url = DONE

        result = cw.drive_consent(page, lambda u: cw.DRIVE_REDIRECT_MARKER in u, email=None, auto_consent=False,
                                  timeout=30, sleep=sleep, clock=clock)
        self.assertEqual(result["status"], "granted")
        self.assertEqual(result["owner_step"], "consent_required")

    def test_code_and_login_hint_helpers(self):
        url = cw.CLI_CODE_PAGE + "?state=x&code=4/0AbC-d_e&scope=email"
        self.assertEqual(cw.code_from_url(url), "4/0AbC-d_e")
        self.assertIsNone(cw.code_from_url("https://example.com/?a=1"))
        hinted = cw.with_login_hint("https://accounts.google.com/o/oauth2/auth?x=1", "a+b@gmail.com")
        self.assertTrue(hinted.endswith("&login_hint=a%2Bb%40gmail.com"))
        self.assertEqual(cw.with_login_hint(hinted, "other@gmail.com"), hinted)


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.patches = [mock.patch.object(gemini_web, "HOME", home),
                        mock.patch.object(gemini_web, "DATA_DIR", home / "data")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()


class RegistryTests(Isolated):
    def test_register_assigns_port_slots_and_feeds_gemini_web(self):
        a = cw.register_account("acct_a", "a@gmail.com", True)
        b = cw.register_account("acct_b", "b@gmail.com", False)
        self.assertEqual((a["port"], b["port"]), (17850, 17852))
        self.assertEqual(gemini_web.account_port("acct_b"), 17852)
        self.assertTrue(cw.account_entry("acct_a")["auto_consent"])
        again = cw.register_account("acct_a", "a2@gmail.com", False)
        self.assertEqual(again["port"], 17850)
        self.assertEqual(cw.account_entry("acct_a")["email"], "a2@gmail.com")

    def test_at_most_five_accounts_and_no_default(self):
        for i in range(5):
            cw.register_account(f"acct{i}", f"{i}@gmail.com", False)
        with self.assertRaises(cw.WorkstationError):
            cw.register_account("acct5", "5@gmail.com", False)
        with self.assertRaises(cw.WorkstationError):
            cw.register_account("default", "x@gmail.com", False)

    def test_unknown_account_is_an_error(self):
        with self.assertRaises(cw.WorkstationError):
            cw.account_entry("nobody")

    def test_cli_home_is_per_account(self):
        cw.register_account("acct_a", "a@gmail.com", False)
        cw.register_account("acct_b", "b@gmail.com", False)
        env_a, env_b = cw.cli_env("acct_a"), cw.cli_env("acct_b")
        self.assertNotEqual(env_a["USERPROFILE"], env_b["USERPROFILE"])
        self.assertEqual(env_a["USERPROFILE"], env_a["HOME"])
        self.assertTrue(str(cw.token_path("acct_a")).startswith(env_a["USERPROFILE"]))


class UsageAndPickTests(unittest.TestCase):
    def test_parse_usage(self):
        text = "Current balance: 2499.98 compute units\nUsage rate: 0.00/hr\nActive assignments: 0\n"
        self.assertEqual(cw.parse_usage(text), {"balance": 2499.98, "active_assignments": 0})
        self.assertEqual(cw.parse_usage(""), {"balance": None, "active_assignments": None})

    def test_pick_prefers_balance_and_skips_busy_or_logged_out(self):
        statuses = [
            {"account": "a", "cli_logged_in": True, "usage": {"balance": 100.0, "active_assignments": 0}},
            {"account": "b", "cli_logged_in": True, "usage": {"balance": 900.0, "active_assignments": 1}},
            {"account": "c", "cli_logged_in": True, "usage": {"balance": 400.0, "active_assignments": 0}},
            {"account": "d", "cli_logged_in": False},
        ]
        self.assertEqual(cw.pick_account(statuses), "c")
        with self.assertRaises(cw.WorkstationError):
            cw.pick_account(statuses[1:2] + statuses[3:])


class ProfileTests(unittest.TestCase):
    def test_sample_profile_is_hf_only(self):
        profile = cw.load_profile(Path(__file__).with_name("profiles") / "qwen-image.json")
        self.assertFalse(cw.needs_hf_token(profile))
        self.assertTrue(cw.needs_drive(profile))  # outputs go back to Drive
        self.assertEqual(len(profile["hf"][0]["files"]), 3)

    def test_bad_drive_mode_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps({"drive": [{"src": "x", "dest": "/y", "mode": "zip"}]}), encoding="utf-8")
            with self.assertRaises(cw.WorkstationError):
                cw.load_profile(path)

    def test_stage_script_carries_plan_but_never_a_token(self):
        profile = {"hf": [{"repo": "org/gated", "files": ["w.safetensors"], "dest": "/content/models",
                           "gated": True}],
                   "drive": [{"src": "AI_Workstation/workflows.tar", "dest": "/content/wf", "mode": "archive"}]}
        with mock.patch.dict("os.environ", {"HF_TOKEN": "hf_SECRET123"}):
            script = cw.stage_script(profile)
        self.assertNotIn("hf_SECRET123", script)
        self.assertIn("org/gated", script)
        self.assertIn("AI_Workstation/workflows.tar", script)
        compile(script, "stage", "exec")
        plan = json.loads(eval(re.search(r"PLAN = json\.loads\((.*)\)\n", script).group(1)))
        self.assertEqual(plan["my_drive"], "/content/drive/MyDrive")
        self.assertEqual(plan["token_upload"], cw.REMOTE_TOKEN_UPLOAD)

    def test_local_token_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "token").write_text("hf_from_file\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"HF_HOME": tmp}, clear=False):
                with mock.patch.dict("os.environ", {"HF_TOKEN": ""}):
                    self.assertEqual(cw.local_hf_token(), "hf_from_file")
                with mock.patch.dict("os.environ", {"HF_TOKEN": "hf_env"}):
                    self.assertEqual(cw.local_hf_token(), "hf_env")

    def test_parse_tagged(self):
        out = 'noise\nSTAGE {"kind": "hf", "GB": 7.26}\nSTAGE not-json\nSTAGE {"kind": "done"}\n'
        self.assertEqual([r["kind"] for r in cw.parse_tagged(out, "STAGE")], ["hf", "done"])


class IdleTests(unittest.TestCase):
    def test_idle_window_and_busy_guard(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(minutes=30)).isoformat()
        self.assertTrue(cw.idle_expired({"last_used": old, "idle_minutes": 20}, now))
        self.assertFalse(cw.idle_expired({"last_used": old, "idle_minutes": 45}, now))
        busy = (now + timedelta(minutes=5)).isoformat()
        self.assertFalse(cw.idle_expired({"last_used": old, "idle_minutes": 20, "busy_until": busy}, now))
        stale = (now - timedelta(minutes=1)).isoformat()
        self.assertTrue(cw.idle_expired({"last_used": old, "idle_minutes": 20, "busy_until": stale}, now))
        self.assertFalse(cw.idle_expired({}, now))
        self.assertFalse(cw.idle_expired({"last_used": old, "idle_minutes": 0}, now))


class StateTests(Isolated):
    def test_touch_never_resurrects_a_released_session(self):
        cw.register_account("acct_a", "a@gmail.com", False)
        cw.update_session("acct_a", "s1", idle_minutes=20)
        cw.drop_session("acct_a", "s1")
        cw.touch("acct_a", "s1")
        cw.mark_busy("acct_a", "s1", 60)
        self.assertEqual(cw.load_state("acct_a")["sessions"], {})


class FakeCli:
    """Records colab-cli calls made through run_cli/check_cli."""

    def __init__(self, outputs=None):
        self.calls, self.outputs = [], outputs or {}

    def __call__(self, account, *args, timeout=None):
        self.calls.append(args)
        out = self.outputs.get(args[0], "")
        return mock.Mock(returncode=0, stdout=out, stderr="")


class FlowTests(Isolated):
    def setUp(self):
        super().setUp()
        cw.register_account("acct_a", "a@gmail.com", True)
        cw.token_path("acct_a").parent.mkdir(parents=True)
        cw.token_path("acct_a").write_text("{}", encoding="utf-8")
        self.cli = FakeCli({"exec": 'STAGE {"kind": "done", "disk_free_GB": 240}\n'})
        self.patch_cli = mock.patch.object(cw, "run_cli", self.cli)
        self.patch_cli.start()
        self.profile = Path(self.tmp.name) / "p.json"

    def tearDown(self):
        self.patch_cli.stop()
        super().tearDown()

    def write_profile(self, profile):
        self.profile.write_text(json.dumps(profile), encoding="utf-8")

    def test_up_hf_only_skips_mount_and_token(self):
        self.write_profile({"hf": [{"repo": "o/r", "files": ["a"], "dest": "/content/models"}]})
        with mock.patch.object(cw, "mount_drive") as mount, mock.patch.object(cw, "spawn_watcher", return_value=42):
            result = cw.up("acct_a", self.profile, gpu="L4", session="s1")
        mount.assert_not_called()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.cli.calls[0], ("new", "-s", "s1", "--gpu", "L4"))
        self.assertFalse(any(call[0] == "upload" for call in self.cli.calls))
        record = cw.load_state("acct_a")["sessions"]["s1"]
        self.assertEqual(record["watcher_pid"], 42)
        self.assertIsNone(record["busy_until"])

    def test_up_gated_uploads_token_file_not_code(self):
        self.write_profile({"hf": [{"repo": "o/r", "files": ["a"], "dest": "/m", "gated": True}],
                            "drive": [{"src": "AI_Workstation/lora.safetensors", "dest": "/m/lora.safetensors"}]})
        uploaded = {}
        real = self.cli.__call__

        def cli(account, *args, timeout=None):
            if args[0] == "upload":
                uploaded["content"] = Path(args[3]).read_text(encoding="utf-8")
                uploaded["remote"] = args[4]
            if args[0] == "exec":
                uploaded["exec_code"] = Path(args[-1]).read_text(encoding="utf-8")
            return real(account, *args, timeout=timeout)

        with mock.patch.object(cw, "run_cli", cli), \
                mock.patch.object(cw, "mount_drive", return_value={"status": "mounted"}) as mount, \
                mock.patch.object(cw, "spawn_watcher", return_value=1), \
                mock.patch.dict("os.environ", {"HF_TOKEN": "hf_SECRET123"}):
            result = cw.up("acct_a", self.profile, session="s2")
        self.assertEqual(result["status"], "ready")
        mount.assert_called_once_with("acct_a", "s2")
        self.assertEqual(uploaded["content"], "hf_SECRET123")
        self.assertEqual(uploaded["remote"], cw.REMOTE_TOKEN_UPLOAD)
        self.assertNotIn("hf_SECRET123", uploaded["exec_code"])
        self.assertTrue(cw.load_state("acct_a")["sessions"]["s2"]["mounted"])

    def test_gated_without_token_fails_before_any_runtime(self):
        self.write_profile({"hf": [{"repo": "o/r", "files": ["a"], "dest": "/m", "gated": True}]})
        with mock.patch.dict("os.environ", {"HF_TOKEN": "", "HF_HOME": self.tmp.name}):
            with self.assertRaises(cw.WorkstationError):
                cw.up("acct_a", self.profile)
        self.assertEqual(self.cli.calls, [])

    def test_down_syncs_outputs_then_stops(self):
        cw.update_session("acct_a", "s1", mounted=True,
                          outputs={"local": "/content/outputs", "drive": "AI_Workstation/outputs"})
        result = cw.down("acct_a", "s1")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual([c[0] for c in self.cli.calls], ["exec", "stop"])
        self.assertEqual(cw.load_state("acct_a")["sessions"], {})

    def test_run_copies_outputs_to_drive_without_unmounting(self):
        cw.update_session("acct_a", "s1", mounted=True,
                          outputs={"local": "/content/outputs", "drive": "AI_Workstation/outputs"})
        codes = []
        real = self.cli.__call__

        def cli(account, *args, timeout=None):
            if args[0] == "exec":
                codes.append(Path(args[-1]).read_text(encoding="utf-8"))
            return real(account, *args, timeout=timeout)

        job = Path(self.tmp.name) / "job.py"
        job.write_text("print(1)", encoding="utf-8")
        with mock.patch.object(cw, "run_cli", cli):
            result = cw.run("acct_a", "s1", job)
        self.assertEqual(len(codes), 2)
        self.assertIn("local, target, unmount = '/content/outputs', "
                      "'/content/drive/MyDrive/AI_Workstation/outputs/s1', False", codes[1])
        self.assertEqual(result["sync"], {})

    def test_run_without_outputs_config_skips_sync(self):
        cw.update_session("acct_a", "s1", mounted=False)
        job = Path(self.tmp.name) / "job.py"
        job.write_text("print(1)", encoding="utf-8")
        result = cw.run("acct_a", "s1", job)
        self.assertEqual([c[0] for c in self.cli.calls], ["exec"])
        self.assertIsNone(result["sync"])

    def test_failed_job_still_syncs_what_it_wrote(self):
        cw.update_session("acct_a", "s1", mounted=True,
                          outputs={"local": "/content/outputs", "drive": "AI_Workstation/outputs"})
        calls = []

        def cli(account, *args, timeout=None):
            calls.append(args[0])
            failed = len(calls) == 1
            return mock.Mock(returncode=1 if failed else 0, stdout="Traceback: boom" if failed else
                             'SYNC {"copied": 2}', stderr="")

        job = Path(self.tmp.name) / "job.py"
        job.write_text("raise SystemExit(1)", encoding="utf-8")
        with mock.patch.object(cw, "run_cli", cli):
            with self.assertRaises(cw.WorkstationError) as ctx:
                cw.run("acct_a", "s1", job)
        self.assertEqual(calls, ["exec", "exec"])
        self.assertIn('"copied": 2', str(ctx.exception))

    def test_down_keeps_runtime_when_sync_fails(self):
        cw.update_session("acct_a", "s1", mounted=True,
                          outputs={"local": "/content/outputs", "drive": "AI_Workstation/outputs"})
        with mock.patch.object(cw, "exec_code", side_effect=cw.WorkstationError("boom")):
            result = cw.down("acct_a", "s1")
        self.assertEqual(result["status"], "sync_failed_runtime_kept")
        self.assertFalse(any(c[0] == "stop" for c in self.cli.calls))
        self.assertIn("s1", cw.load_state("acct_a")["sessions"])

    def test_up_idle_zero_spawns_no_watcher(self):
        self.write_profile({"hf": [{"repo": "o/r", "files": ["a"], "dest": "/content/models"}]})
        with mock.patch.object(cw, "spawn_watcher") as spawn:
            result = cw.up("acct_a", self.profile, session="s1", idle_minutes=0)
        spawn.assert_not_called()
        self.assertEqual(result["status"], "ready")

    def test_exec_on_reclaimed_runtime_drops_session(self):
        cw.update_session("acct_a", "s1", idle_minutes=20)
        lost = mock.Mock(returncode=1, stdout="", stderr="[colab] Session 's1' appears to be lost (404/401). Cleaning up.")
        with mock.patch.object(cw, "run_cli", return_value=lost):
            with self.assertRaises(cw.RuntimeLost):
                cw.exec_code("acct_a", "s1", "print(1)", 60)
        self.assertEqual(cw.load_state("acct_a")["sessions"], {})

    def test_job_past_timeout_kills_cli_and_restarts_kernel(self):
        cw.update_session("acct_a", "s1", idle_minutes=20)
        calls = []

        def cli(account, *args, timeout=None):
            calls.append((args[0], timeout))
            if args[0] == "exec":
                raise cw.subprocess.TimeoutExpired("colab", timeout)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(cw, "run_cli", cli):
            with self.assertRaises(cw.JobTimeout):
                cw.exec_code("acct_a", "s1", "import time; time.sleep(999)", 30)
        self.assertEqual(calls, [("exec", 30), ("restart-kernel", 120)])
        record = cw.load_state("acct_a")["sessions"]["s1"]
        self.assertIsNone(record["busy_until"])

    def test_second_exec_refused_while_another_process_runs_a_job(self):
        cw.update_session("acct_a", "s1", idle_minutes=20, busy_pid=999999,
                          busy_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        with mock.patch("psutil.pid_exists", return_value=True):
            with self.assertRaises(cw.WorkstationError) as ctx:
                cw.exec_code("acct_a", "s1", "print(1)", 60)
        self.assertIn("busy", str(ctx.exception))
        self.assertEqual(self.cli.calls, [])
        with mock.patch("psutil.pid_exists", return_value=False):  # crashed runner: stale busy mark
            cw.exec_code("acct_a", "s1", "print(1)", 60)
        self.assertEqual(self.cli.calls[0][0], "exec")

    def test_job_error_mentioning_not_found_keeps_session(self):
        cw.update_session("acct_a", "s1", idle_minutes=20)
        failed = mock.Mock(returncode=1, stdout="FileNotFoundError: /content/x not found\nHTTP 404", stderr="")
        with mock.patch.object(cw, "run_cli", return_value=failed):
            with self.assertRaises(cw.WorkstationError) as ctx:
                cw.exec_code("acct_a", "s1", "print(1)", 60)
        self.assertNotIsInstance(ctx.exception, cw.RuntimeLost)
        self.assertIn("s1", cw.load_state("acct_a")["sessions"])

    def test_watch_releases_idle_session(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        cw.update_session("acct_a", "s1", last_used=old, idle_minutes=20)
        result = cw.watch("acct_a", "s1", sleep=lambda _: None)
        self.assertEqual(result["reason"], "idle")
        self.assertEqual(self.cli.calls[-1], ("stop", "-s", "s1"))
        self.assertEqual(cw.watch("acct_a", "s1", sleep=lambda _: None)["status"], "gone")


class LoginFlowTests(Isolated):
    def test_login_feeds_code_from_bridge(self):
        cw.register_account("acct_a", "a@gmail.com", True)
        fake = mock.Mock()
        fake.wait_for.return_value = re.search(cw.LOGIN_URL_PATTERN, "  https://accounts.google.com/o/oauth2/auth?x=1")
        fake.finish.return_value = 0
        fake.output = "Current balance: 10.0 compute units\nActive assignments: 0"
        seen = {}

        def bridge(account, url, done, timeout):
            seen["url"] = url
            final = cw.CLI_CODE_PAGE + "?code=4/0XYZ"
            self.assertTrue(done(final))
            return {"status": "granted", "final_url": final}

        with mock.patch.object(cw, "InteractiveCli", return_value=fake):
            result = cw.login("acct_a", consent=bridge)
        self.assertEqual(result["status"], "logged_in")
        self.assertIn("login_hint=a%40gmail.com", seen["url"])
        fake.send.assert_called_once_with("4/0XYZ")


class CliTests(Isolated):
    def test_error_is_json_and_exit_1(self):
        with mock.patch("builtins.print") as printed:
            code = cw.main(["login", "--account", "nobody"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(printed.call_args[0][0])["status"], "error")

    def test_parser_knows_all_commands(self):
        parser = cw.build_arg_parser()
        for argv in (["status"], ["onboard", "--account", "a", "--email", "a@b.c", "--auto-consent"],
                     ["up", "--account", "auto", "--profile", "p.json", "--gpu", "L4"],
                     ["run", "--account", "a", "--session", "s", "-f", "job.py"],
                     ["down", "--account", "a", "--session", "s", "--no-sync"],
                     ["pack", "--account", "a", "--session", "s", "--src", "x", "--out", "x.tar"]):
            parser.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
