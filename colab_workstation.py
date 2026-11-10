#!/usr/bin/env python3
"""On-demand Colab GPU workstation for 1-5 Google accounts, driven by the official Colab CLI.

Each account gets its own colab-cli home (token.json + sessions.json, isolated through USERPROFILE)
and its own Gemini Web Chrome bridge (gemini_web.py --account). The browser is only needed for two
Google consent pages, and both open in the account's bridge in the background:

  * login: the CLI's one-time OAuth (the refresh token is reused afterwards);
  * mount: Drive access for each new runtime (Colab asks again for every fresh VM).

A run is: pick an account -> `up` (new runtime, mount Drive if the profile needs it, stage weights)
-> `run` jobs (each followed by an incremental copy of new outputs to Drive) -> `down` (last copy,
Drive flushed, runtime released). A detached watcher releases a runtime left idle longer than
--idle-minutes (0 = never; Colab's own idle reclaim and lifetime cap still apply).

  python -X utf8 colab_workstation.py install-cli [--upgrade]
  python -X utf8 colab_workstation.py status [--account NAME] [--no-usage]
  python -X utf8 colab_workstation.py onboard --account NAME --email ADDR [--auto-consent]
  python -X utf8 colab_workstation.py login --account NAME
  python -X utf8 colab_workstation.py up --account NAME|auto --profile P.json [--gpu L4] [--session S] [--idle-minutes 20]
  python -X utf8 colab_workstation.py run --account NAME --session S -f job.py [--timeout SEC] [--no-sync]
  python -X utf8 colab_workstation.py fetch --account NAME --session S REMOTE LOCAL
  python -X utf8 colab_workstation.py pack --account NAME --session S --src AI_Workstation/workflows --out AI_Workstation/workflows.tar
  python -X utf8 colab_workstation.py down --account NAME --session S [--no-sync]

Staging rules (why the profile has two lists):
  * hf: public or gated Hugging Face weights download straight onto the VM's local disk (measured
    113-158 MB/s, about 2x faster than reading the same files back from Drive at 51-70 MB/s). A
    gated repo needs HF_TOKEN: the local token is uploaded as a file into the VM's
    ~/.cache/huggingface/token (mode 600). It is never put in code, `--env`, a notebook or Drive,
    because colab-cli logs executed code in plain text under ~/.config/colab-cli/history.
  * drive: private assets. mode "copy" for large single files (LoRAs, private checkpoints): a tar
    gains nothing for already-compressed tensors. mode "archive" for trees of many small files
    (workflows, custom nodes): Drive's FUSE mount pays a round trip per file, so they live on
    Drive as one .tar that is extracted locally (`pack` builds it).
  * outputs: written to the VM's local disk and copied to Drive after every `run` (new or changed
    files only), so a runtime Colab reclaims on its own loses nothing; `down` copies once more and
    flushes Drive before releasing (the VM disk is wiped when the runtime goes away). `fetch` is
    only for pulling a single file to this machine.

Consent boundary: password and 2-step sign-in pages, "Verify it's you" challenges and CAPTCHAs are
never automated; the bridge window is brought to the front for the owner. Google's OAuth consent
screens for the CLI and for Drive are clicked (including "Select all") only for accounts onboarded
with --auto-consent; otherwise the window comes forward and the run waits for the owner's click.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterable
import urllib.parse

import gemini_web

MAX_ACCOUNTS = 5
ACCOUNT_PORT_SLOTS = (17850, 17852, 17853, 17854, 17855)
DRIVE_MOUNT = "/content/drive"
MY_DRIVE = DRIVE_MOUNT + "/MyDrive"
DEFAULT_IDLE_MINUTES = 20
GPUS = ("T4", "L4", "G4", "A100", "H100")
CLI_LOGIN_TIMEOUT = 300
CONSENT_TIMEOUT = 300
REMOTE_TOKEN_UPLOAD = "content/.hf_token_upload"
OK_STATUSES = ("ready", "stopped", "logged_in", "already_logged_in", "gone", "mounted", "installed")

LOGIN_URL_PATTERN = re.compile(r"(https://accounts\.google\.com/o/oauth2/auth\S+)")
DRIVE_URL_PATTERN = re.compile(r"(https://accounts\.google\.com/o/oauth2/v2/auth\S+)")
CLI_CODE_PAGE = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
DRIVE_REDIRECT_MARKER = "colab.research.google.com/tun/m/authorize-for-drive"
BALANCE_PATTERN = re.compile(r"Current balance:\s*([\d.]+)")
ACTIVE_PATTERN = re.compile(r"Active assignments:\s*(\d+)")
EMAIL_PATTERN = re.compile(r"Email:\s*(\S+@\S+)")

# Identity checks Google runs before it trusts the session: always the owner's job.
SECURITY_CHALLENGE_TEXTS = ("Verify it's you", "Verify that it's you", "This browser or app may not be secure",
                            "Couldn't sign you in")
CAPTCHA_TEXTS = ("I'm not a robot", "unusual traffic", "Confirm you're not a robot")
CHALLENGE_URL_MARKERS = ("/signin/challenge", "/signin/v2/challenge", "google.com/sorry", "/recaptcha/")
SIGN_IN_SELECTORS = ('input[type="password"]', 'input[type="email"]', "#identifierId")
CONSENT_BUTTON = re.compile(r"^\s*(Continue|Allow|继续|允许)\s*$", re.IGNORECASE)
SELECT_ALL = re.compile(r"^\s*(Select all|全选)\s*$", re.IGNORECASE)


class WorkstationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- account registry


def registry_path() -> Path:
    """Shared with gemini_web: its port lookup reads the same <HOME>/accounts.json."""
    return gemini_web.HOME / "accounts.json"


def load_accounts() -> dict[str, dict[str, Any]]:
    accounts = _read_json(registry_path()).get("accounts")
    return {name: dict(entry) for name, entry in accounts.items() if isinstance(entry, dict)} \
        if isinstance(accounts, dict) else {}


def account_entry(account: str) -> dict[str, Any]:
    name = gemini_web.normalize_account(account)
    entry = load_accounts().get(name)
    if entry is None:
        raise WorkstationError(f"Account {name!r} is not onboarded; run: onboard --account {name} --email ADDR")
    return {"name": name, **entry}


def register_account(account: str, email: str, auto_consent: bool) -> dict[str, Any]:
    name = gemini_web.normalize_account(account)
    if name == gemini_web.DEFAULT_ACCOUNT:
        raise WorkstationError("Use a named account (e.g. akiiwa), not the default Gemini bridge")
    path = registry_path()
    data = _read_json(path)
    accounts = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    if name not in accounts and len(accounts) >= MAX_ACCOUNTS:
        raise WorkstationError(f"At most {MAX_ACCOUNTS} accounts; remove one from {path} first")
    entry = dict(accounts.get(name) or {})
    if "port" not in entry:
        used = {e.get("port") for e in accounts.values() if isinstance(e, dict)}
        free = [port for port in ACCOUNT_PORT_SLOTS if port not in used]
        if not free:
            raise WorkstationError("No free CDP port slot")
        entry["port"] = free[0]
    entry.update(email=email, auto_consent=bool(auto_consent))
    accounts[name] = entry
    data["accounts"] = accounts
    atomic_write_json(path, data)
    return {"name": name, **entry}


# --------------------------------------------------------------------------- colab-cli plumbing


def bundled_cli_dir() -> Path:
    return gemini_web.DATA_DIR / "colab-cli"


def colab_exe() -> str:
    """COLAB_CLI, then the private venv made by `install-cli`, then `colab` on PATH."""
    bundled = bundled_cli_dir() / ("Scripts/colab.exe" if os.name == "nt" else "bin/colab")
    found = os.environ.get("COLAB_CLI") or (str(bundled) if bundled.is_file() else None) or shutil.which("colab")
    if not found:
        raise WorkstationError("Colab CLI not found: run `install-cli` (or set COLAB_CLI to colab.exe)")
    return found


def install_cli(upgrade: bool = False) -> dict[str, Any]:
    """google-colab-cli in its own venv, so its pins never touch the caller's Python."""
    venv = bundled_cli_dir()
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    args = [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "google-colab-cli"]
    proc = subprocess.run(args + (["--upgrade"] if upgrade else []), capture_output=True, text=True)
    if proc.returncode != 0:
        raise WorkstationError(f"pip install google-colab-cli failed: {proc.stderr[-1500:]}")
    version = subprocess.run([colab_exe(), "version"], capture_output=True, text=True, env=cli_env_base())
    return {"status": "installed", "venv": str(venv), "version": version.stdout.strip()}


def cli_env_base() -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def cli_home(account: str) -> Path:
    return gemini_web.account_config(account).home / "colab-cli"


def cli_env(account: str) -> dict[str, str]:
    home = str(cli_home(account))
    return {**cli_env_base(), "USERPROFILE": home, "HOME": home}


def token_path(account: str) -> Path:
    return cli_home(account) / ".config" / "colab-cli" / "token.json"


def run_cli(account: str, *args: str, timeout: float | None = 120) -> subprocess.CompletedProcess:
    cli_home(account).mkdir(parents=True, exist_ok=True)
    return subprocess.run([colab_exe(), *args], env=cli_env(account), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


class CliTimeout(WorkstationError):
    pass


def check_cli(account: str, *args: str, timeout: float | None = 120) -> str:
    try:
        proc = run_cli(account, *args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CliTimeout(f"colab {' '.join(args)} did not finish within {timeout:.0f}s") from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise WorkstationError(f"colab {' '.join(args)} failed ({proc.returncode}): {output.strip()[-2000:]}")
    return output


class InteractiveCli:
    """A colab-cli process whose prompts are answered from code (OAuth code, "press Enter")."""

    def __init__(self, account: str, *args: str):
        cli_home(account).mkdir(parents=True, exist_ok=True)
        self.lines: list[str] = []
        self._queue: queue.Queue[str | None] = queue.Queue()
        self.proc = subprocess.Popen([colab_exe(), *args], env=cli_env(account), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self._queue.put(line.rstrip("\n"))
        self._queue.put(None)

    def wait_for(self, pattern: re.Pattern[str], timeout: float) -> re.Match[str] | None:
        """First match in the output, or None once the process ends or `timeout` passes."""
        deadline = time.monotonic() + timeout
        while True:
            for line in self.lines:
                found = pattern.search(line)
                if found:
                    return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                return None
            self.lines.append(line)

    def send(self, text: str) -> None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def finish(self, timeout: float) -> int:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        while True:
            try:
                line = self._queue.get(timeout=1.0)
            except queue.Empty:
                break
            if line is None:
                break
            self.lines.append(line)
        return code

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------- consent in the bridge


def _visible(locator: Any) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def detect_blocker(page: Any) -> str | None:
    """Pages only the owner may complete: sign-in, security challenge, CAPTCHA."""
    url = (page.url or "").lower()
    if any(marker in url for marker in CHALLENGE_URL_MARKERS):
        return "captcha" if ("sorry" in url or "recaptcha" in url) else "security_challenge"
    for text in SECURITY_CHALLENGE_TEXTS:
        if _visible(page.get_by_text(text)):
            return "security_challenge"
    for text in CAPTCHA_TEXTS:
        if _visible(page.get_by_text(text)):
            return "captcha"
    if any(_visible(page.locator(selector)) for selector in SIGN_IN_SELECTORS):
        return "sign_in"
    return None


def click_consent(page: Any, email: str | None) -> str | None:
    """One step through Google's consent screens; returns what was clicked."""
    if email and "accountchooser" in (page.url or "").lower():
        chooser = page.locator(f'[data-identifier="{email}"]')
        if _visible(chooser):
            chooser.first.click()
            return "account"
    select_all = page.get_by_role("checkbox", name=SELECT_ALL)
    if _visible(select_all):
        try:
            checked = select_all.first.is_checked()
        except Exception:
            checked = False
        if not checked:
            select_all.first.check()
            return "select_all"
    button = page.get_by_role("button", name=CONSENT_BUTTON)
    if _visible(button):
        target = button.last
        try:
            target.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        if target.is_enabled():
            target.click()
            return "continue"
    return None


def drive_consent(page: Any, done: Callable[[str], bool], *, email: str | None, auto_consent: bool,
                  timeout: float = CONSENT_TIMEOUT, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Walk an OAuth flow already loaded in `page` until `done(url)`; owner-only pages come to the front."""
    deadline = clock() + timeout
    surfaced: str | None = None
    clicks: list[str] = []
    while clock() < deadline:
        if done(page.url or ""):
            return {"status": "granted", "url": page.url, "clicks": clicks, "owner_step": surfaced}
        blocker = detect_blocker(page)
        if blocker or not auto_consent:
            reason = blocker or "consent_required"
            if surfaced != reason:
                gemini_web.bring_to_front(page)
                surfaced = reason
        else:
            try:
                clicked = click_consent(page, email)
            except Exception:
                clicked = None
            if clicked:
                clicks.append(clicked)
                sleep(1.5)
                continue
        sleep(1.0)
    return {"status": surfaced or "timeout", "url": page.url, "clicks": clicks}


def _sync_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def consent_in_bridge(account: str, url: str, done: Callable[[str], bool], timeout: float = CONSENT_TIMEOUT
                      ) -> dict[str, Any]:
    entry = account_entry(account)
    with _sync_playwright() as playwright:
        _browser, page = gemini_web.new_background_page(playwright, url, account=entry["name"])
        try:
            result = drive_consent(page, done, email=entry.get("email"),
                                   auto_consent=bool(entry.get("auto_consent")), timeout=timeout)
            result["final_url"] = page.url
            return result
        finally:
            try:
                page.close()
            except Exception:
                pass


def with_login_hint(url: str, email: str | None) -> str:
    if not email or "login_hint=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "login_hint=" + urllib.parse.quote(email)


def code_from_url(url: str) -> str | None:
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("code")
    return values[0] if values else None


# --------------------------------------------------------------------------- login / mount


def login(account: str, consent: Callable[..., dict[str, Any]] = consent_in_bridge) -> dict[str, Any]:
    """The CLI's OAuth (copy-paste flow), with the code read from the bridge tab instead of a human."""
    entry = account_entry(account)
    cli = InteractiveCli(entry["name"], "usage")
    found = cli.wait_for(LOGIN_URL_PATTERN, 60)
    if not found:
        cli.finish(30)
        if cli.proc.returncode == 0:
            return {"account": entry["name"], "status": "already_logged_in", "usage": parse_usage(cli.output)}
        raise WorkstationError(f"colab-cli did not print a login URL:\n{cli.output[-2000:]}")
    url = with_login_hint(found.group(1), entry.get("email"))
    result = consent(entry["name"], url, lambda u: u.startswith(CLI_CODE_PAGE) and bool(code_from_url(u)),
                     CLI_LOGIN_TIMEOUT)
    code = code_from_url(result.get("final_url") or result.get("url") or "")
    if result.get("status") != "granted" or not code:
        cli.proc.kill()
        return {"account": entry["name"], "status": result.get("status", "failed"), "consent": result}
    cli.send(code)
    rc = cli.finish(120)
    if rc != 0:
        raise WorkstationError(f"colab-cli rejected the authorization code:\n{cli.output[-2000:]}")
    return {"account": entry["name"], "status": "logged_in", "usage": parse_usage(cli.output)}


def mount_drive(account: str, session: str, consent: Callable[..., dict[str, Any]] = consent_in_bridge
                ) -> dict[str, Any]:
    """`colab drivemount`: each new VM asks for Drive access again; grant it in the bridge, then press Enter."""
    entry = account_entry(account)
    started = time.monotonic()
    cli = InteractiveCli(entry["name"], "drivemount", "-s", session)
    found = cli.wait_for(re.compile(DRIVE_URL_PATTERN.pattern + r"|Mounted at"), 180)
    consent_result = None
    if found and found.group(1):
        consent_result = consent(entry["name"], found.group(1), lambda u: DRIVE_REDIRECT_MARKER in u)
        if consent_result.get("status") != "granted":
            cli.proc.kill()
            return {"status": consent_result.get("status", "failed"), "consent": consent_result}
        cli.send("")
    rc = cli.finish(240)
    mounted = "Mounted at" in cli.output
    if rc != 0 or not mounted:
        raise WorkstationError(f"Drive mount failed:\n{cli.output[-2000:]}")
    return {"status": "mounted", "seconds": round(time.monotonic() - started), "consent": consent_result}


# --------------------------------------------------------------------------- usage / status / pick


def parse_usage(text: str) -> dict[str, Any]:
    balance = BALANCE_PATTERN.search(text or "")
    active = ACTIVE_PATTERN.search(text or "")
    return {"balance": float(balance.group(1)) if balance else None,
            "active_assignments": int(active.group(1)) if active else None}


def account_status(account: str, usage: bool = True) -> dict[str, Any]:
    entry = account_entry(account)
    name = entry["name"]
    cfg = gemini_web.account_config(name)
    info: dict[str, Any] = {"account": name, "email": entry.get("email"), "auto_consent": bool(entry.get("auto_consent")),
                            "bridge": {"cdp": cfg.endpoint, "running": bool(gemini_web.health(account=name))},
                            "cli_logged_in": token_path(name).is_file(), "sessions": load_state(name).get("sessions", {})}
    if usage and info["cli_logged_in"]:
        try:
            info["usage"] = parse_usage(check_cli(name, "usage", timeout=60))
        except (WorkstationError, subprocess.TimeoutExpired) as exc:
            info["usage_error"] = str(exc)[:300]
    return info


def status(accounts: list[str] | None = None, usage: bool = True) -> dict[str, Any]:
    names = accounts or list(load_accounts())
    return {"checked_at": utc_now(), "registry": str(registry_path()),
            "accounts": [account_status(name, usage=usage) for name in names]}


def pick_account(statuses: Iterable[dict[str, Any]]) -> str:
    """`--account auto`: the logged-in account with the most compute units and no runtime of its own."""
    ready = [s for s in statuses if s.get("cli_logged_in") and (s.get("usage") or {}).get("balance") is not None
             and not (s.get("usage") or {}).get("active_assignments")]
    if not ready:
        raise WorkstationError("No logged-in account without an active runtime; see `status`")
    return max(ready, key=lambda s: s["usage"]["balance"])["account"]


def onboard(account: str, email: str, auto_consent: bool) -> dict[str, Any]:
    entry = register_account(account, email, auto_consent)
    message = gemini_web.start(show=True, site="drive", account=entry["name"])
    return {**entry, "message": message, "next_steps": [
        f"Sign in to {email} in the window that just opened (once; the profile keeps it).",
        f"Then run: login --account {entry['name']}",
    ]}


# --------------------------------------------------------------------------- per-account session state


def state_path(account: str) -> Path:
    return gemini_web.account_config(account).home / "colab-workstation.json"


def load_state(account: str) -> dict[str, Any]:
    data = _read_json(state_path(account))
    data.setdefault("sessions", {})
    return data


def update_session(account: str, session: str, create: bool = True, **fields: Any) -> dict[str, Any] | None:
    data = load_state(account)
    if session not in data["sessions"] and not create:
        return None  # released meanwhile (down / watcher); do not resurrect it
    record = data["sessions"].setdefault(session, {})
    record.update(fields)
    atomic_write_json(state_path(account), data)
    return record


def drop_session(account: str, session: str) -> None:
    data = load_state(account)
    if data["sessions"].pop(session, None) is not None:
        atomic_write_json(state_path(account), data)


def touch(account: str, session: str) -> None:
    update_session(account, session, create=False, last_used=utc_now(), busy_until=None)


def mark_busy(account: str, session: str, seconds: float) -> None:
    """A job is running: the watcher keeps its hands off until it ends or its timeout passes."""
    until = datetime.fromtimestamp(time.time() + seconds + 120, timezone.utc).isoformat()
    update_session(account, session, create=False, last_used=utc_now(), busy_until=until, busy_pid=os.getpid())


def job_running(record: dict[str, Any], now: datetime | None = None) -> bool:
    """Another live process of ours is executing on this session right now."""
    now = now or datetime.now(timezone.utc)
    try:
        if not record.get("busy_until") or datetime.fromisoformat(record["busy_until"]) <= now:
            return False
    except (TypeError, ValueError):
        return False
    pid = record.get("busy_pid")
    if not pid or pid == os.getpid():
        return False
    import psutil
    return psutil.pid_exists(pid)


# --------------------------------------------------------------------------- profiles and staging


def load_profile(path: str | Path) -> dict[str, Any]:
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    for item in profile.get("drive", []):
        if item.get("mode", "copy") not in ("copy", "archive"):
            raise WorkstationError(f"drive item {item.get('src')!r}: mode must be 'copy' or 'archive'")
    return profile


def needs_drive(profile: dict[str, Any]) -> bool:
    return bool(profile.get("drive") or (profile.get("outputs") or {}).get("drive"))


def needs_hf_token(profile: dict[str, Any]) -> bool:
    return any(item.get("gated") for item in profile.get("hf", []))


def local_hf_token() -> str | None:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        return token.strip()
    path = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "token"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


STAGE_SCRIPT = r'''
import json, os, shutil, subprocess, time
PLAN = json.loads(__PLAN__)

def report(kind, **row):
    print("STAGE", json.dumps({"kind": kind, **row}), flush=True)

upload = "/" + PLAN["token_upload"]
if os.path.exists(upload):
    target = os.path.expanduser("~/.cache/huggingface/token")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.move(upload, target)
    os.chmod(target, 0o600)
    report("hf_token", installed=True)

if PLAN["hf"]:
    from huggingface_hub import hf_hub_download
for item in PLAN["hf"]:
    for name in item["files"]:
        started = time.time()
        path = hf_hub_download(item["repo"], name, revision=item.get("revision"), local_dir=item["dest"])
        seconds = time.time() - started
        gb = os.path.getsize(path) / 1e9
        report("hf", repo=item["repo"], file=name, GB=round(gb, 2), s=round(seconds, 1),
               MBps=round(gb * 1000 / max(seconds, 0.01)))

for item in PLAN["drive"]:
    src = os.path.join(PLAN["my_drive"], item["src"])
    dest = item["dest"]
    started = time.time()
    if item.get("mode", "copy") == "archive":
        os.makedirs(dest, exist_ok=True)
        subprocess.run(["tar", "-xf", src, "-C", dest], check=True)
    elif os.path.isdir(src):
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copyfile(src, dest)
    size = os.path.getsize(src) if os.path.isfile(src) else sum(
        os.path.getsize(os.path.join(root, f)) for root, _, files in os.walk(src) for f in files)
    seconds = time.time() - started
    report("drive", src=item["src"], mode=item.get("mode", "copy"), GB=round(size / 1e9, 3), s=round(seconds, 1))

out = (PLAN.get("outputs") or {}).get("local")
if out:
    os.makedirs(out, exist_ok=True)
report("done", disk_free_GB=round(shutil.disk_usage("/content").free / 1e9))
'''


def stage_script(profile: dict[str, Any]) -> str:
    plan = {"hf": profile.get("hf", []), "drive": profile.get("drive", []), "outputs": profile.get("outputs"),
            "my_drive": MY_DRIVE, "token_upload": REMOTE_TOKEN_UPLOAD}
    return STAGE_SCRIPT.replace("__PLAN__", repr(json.dumps(plan)))


SYNC_SCRIPT = r'''
import json, os, shutil
local, target, unmount = __LOCAL__, __TARGET__, __UNMOUNT__
copied = skipped = 0
if os.path.isdir(local):
    for root, _, files in os.walk(local):
        for name in files:
            src = os.path.join(root, name)
            dst = os.path.join(target, os.path.relpath(src, local))
            st = os.stat(src)
            try:
                old = os.stat(dst)
                if old.st_size == st.st_size and old.st_mtime >= st.st_mtime - 1:
                    skipped += 1
                    continue
            except OSError:
                pass
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            try:
                os.utime(dst, (st.st_atime, st.st_mtime))
            except OSError:
                pass
            copied += 1
if unmount:
    from google.colab import drive
    drive.flush_and_unmount()
print("SYNC", json.dumps({"copied": copied, "skipped": skipped, "target": target, "unmounted": unmount}), flush=True)
'''

PACK_SCRIPT = r'''
import json, os, shutil, subprocess, time
src, out = __SRC__, __OUT__
started = time.time()
tmp = "/content/_pack.tar"
subprocess.run(["tar", "-cf", tmp, "-C", src, "."], check=True)
os.makedirs(os.path.dirname(out), exist_ok=True)
shutil.copyfile(tmp, out)
size = os.path.getsize(tmp)
os.remove(tmp)
print("PACK", json.dumps({"out": out, "MB": round(size / 1e6, 1), "s": round(time.time() - started, 1)}), flush=True)
'''


def parse_tagged(output: str, tag: str) -> list[dict[str, Any]]:
    rows = []
    for line in output.splitlines():
        if line.startswith(tag + " "):
            try:
                rows.append(json.loads(line[len(tag) + 1:]))
            except ValueError:
                pass
    return rows


LOST_PATTERN = re.compile(r"\[colab\] Session '[^']+' (appears to be lost|not found)")


class RuntimeLost(WorkstationError):
    """Colab reclaimed the VM (its own idle timeout or lifetime cap); its local disk is gone."""


class SessionBusy(WorkstationError):
    """Another process of ours is executing on the session."""


class JobTimeout(WorkstationError):
    """The job ran past its timeout; it was stopped by restarting the kernel. The VM disk is kept."""


def exec_file(account: str, session: str, path: str | Path, timeout: float) -> str:
    # colab-cli's --timeout bounds a *silent* stretch, not the job, and once it trips the CLI hangs
    # instead of exiting (measured 2026-11-09). So the wall-clock limit is ours: kill the CLI, then
    # restart the kernel, otherwise the job keeps running unseen and the next exec queues behind it.
    record = load_state(account)["sessions"].get(session) or {}
    if job_running(record):
        raise SessionBusy(f"Session {session} is busy with another job (pid {record['busy_pid']}) until "
                               f"{record['busy_until']}; a second exec would queue behind it")
    mark_busy(account, session, timeout)
    try:
        return check_cli(account, "exec", "-s", session, "--timeout", str(int(timeout)), "-f", str(path),
                         timeout=timeout)
    except CliTimeout as exc:
        try:
            restarted = f"rc={run_cli(account, 'restart-kernel', '-s', session, timeout=120).returncode}"
        except subprocess.TimeoutExpired:
            restarted = "timed out"
        raise JobTimeout(f"Job on {session} exceeded {timeout:.0f}s and was stopped (kernel restart {restarted}); "
                         "files on the VM disk are kept") from exc
    except WorkstationError as exc:
        if LOST_PATTERN.search(str(exc)):
            drop_session(account, session)
            raise RuntimeLost(f"Runtime {session} is gone (reclaimed by Colab); run `up` again. {exc}") from exc
        raise
    finally:
        touch(account, session)


def exec_code(account: str, session: str, code: str, timeout: float) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(code)
        path = handle.name
    try:
        return exec_file(account, session, path, timeout)
    finally:
        os.unlink(path)


def upload_hf_token(account: str, session: str, token: str) -> None:
    """Token travels as a file through the Jupyter contents API; colab-cli logs only the paths."""
    handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
    try:
        handle.write(token)
        handle.close()
        check_cli(account, "upload", "-s", session, handle.name, REMOTE_TOKEN_UPLOAD)
    finally:
        os.unlink(handle.name)


# --------------------------------------------------------------------------- up / run / down


def new_session_name() -> str:
    return datetime.now().strftime("ws-%m%d-%H%M%S")


def up(account: str, profile_path: str | Path, *, gpu: str | None = None, session: str | None = None,
       idle_minutes: int = DEFAULT_IDLE_MINUTES, high_mem: bool = False, watch: bool = True) -> dict[str, Any]:
    profile = load_profile(profile_path)
    if account == "auto":
        account = pick_account(status().get("accounts", []))
    entry = account_entry(account)
    name = entry["name"]
    token = None
    if needs_hf_token(profile):
        token = local_hf_token()
        if not token:
            raise WorkstationError("Profile has gated HF weights but no HF_TOKEN env var or ~/.cache/huggingface/token")
    if not token_path(name).is_file():
        result = login(name)
        if result["status"] not in ("logged_in", "already_logged_in"):
            return {"account": name, "status": "login_" + str(result["status"]), "login": result}
    session = session or new_session_name()
    args = ["new", "-s", session] + (["--gpu", gpu] if gpu else []) + (["--high-mem"] if high_mem else [])
    started = time.monotonic()
    try:
        check_cli(name, *args, timeout=600)
    except WorkstationError:
        # `new` can fail after the VM was assigned (e.g. a backend read timeout while the kernel starts);
        # stop it so a half-made runtime does not bill unseen.
        try:
            run_cli(name, "stop", "-s", session, timeout=120)
        except subprocess.TimeoutExpired:
            pass
        raise
    update_session(name, session, created_at=utc_now(), last_used=utc_now(), gpu=gpu or "cpu",
                   profile=str(Path(profile_path).resolve()), idle_minutes=idle_minutes,
                   outputs=profile.get("outputs"), mounted=False)
    result: dict[str, Any] = {"account": name, "session": session, "gpu": gpu or "cpu",
                              "new_seconds": round(time.monotonic() - started), "idle_minutes": idle_minutes}
    # From here on the runtime bills; the watcher releases it even if staging fails.
    if watch and idle_minutes > 0:
        result["watcher_pid"] = spawn_watcher(name, session)
        update_session(name, session, watcher_pid=result["watcher_pid"])
    try:
        if needs_drive(profile):
            result["mount"] = mount_drive(name, session)
            if result["mount"]["status"] != "mounted":
                return {**result, "status": "mount_" + result["mount"]["status"]}
            update_session(name, session, mounted=True)
        if token:
            upload_hf_token(name, session, token)
        result["stage"] = parse_tagged(exec_code(name, session, stage_script(profile), timeout=3600), "STAGE")
    except WorkstationError as exc:
        return {**result, "status": "stage_failed", "error": str(exc)[-2000:],
                "hint": f"runtime {session} is still up: fix and `run`, or `down`"}
    finally:
        touch(name, session)
    return {**result, "status": "ready"}


def sync_outputs(account: str, session: str, *, unmount: bool = False) -> dict[str, Any] | None:
    """Copy new/changed files from outputs.local to <outputs.drive>/<session>/; None when not configured."""
    record = load_state(account)["sessions"].get(session) or {}
    outputs = record.get("outputs") or {}
    if not (record.get("mounted") and outputs.get("local") and outputs.get("drive")):
        return None
    target = f"{MY_DRIVE}/{outputs['drive'].strip('/')}/{session}"
    code = (SYNC_SCRIPT.replace("__LOCAL__", repr(outputs["local"])).replace("__TARGET__", repr(target))
            .replace("__UNMOUNT__", repr(unmount)))
    rows = parse_tagged(exec_code(account, session, code, timeout=3600), "SYNC")
    return rows[-1] if rows else {}


def sync_quietly(account: str, session: str) -> dict[str, Any] | None:
    try:
        return sync_outputs(account, session)
    except WorkstationError as exc:
        return {"error": str(exc)[-500:]}


def run(account: str, session: str, file: str | Path, timeout: float = 3600, sync: bool = True) -> dict[str, Any]:
    """Execute a job; afterwards its outputs go to Drive, so a runtime Colab reclaims later loses nothing."""
    name = account_entry(account)["name"]
    started = time.monotonic()
    try:
        output = exec_file(name, session, file, timeout)
    except (RuntimeLost, SessionBusy):
        raise
    except WorkstationError as exc:
        synced = sync_quietly(name, session) if sync else None  # keep what the failed job wrote
        raise type(exc)(f"{exc}\n[outputs sync: {json.dumps(synced)}]") from exc
    result = {"account": name, "session": session, "seconds": round(time.monotonic() - started), "output": output}
    if sync:
        result["sync"] = sync_quietly(name, session)
    return result


def fetch(account: str, session: str, remote: str, local: str | Path) -> dict[str, Any]:
    name = account_entry(account)["name"]
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    mark_busy(name, session, 1800)
    try:
        check_cli(name, "download", "-s", session, remote, str(local), timeout=1800)
    finally:
        touch(name, session)
    return {"account": name, "session": session, "remote": remote, "local": str(local)}


def pack(account: str, session: str, src: str, out: str) -> dict[str, Any]:
    """Turn a Drive folder of many small files into one Drive .tar for mode "archive"."""
    name = account_entry(account)["name"]
    code = PACK_SCRIPT.replace("__SRC__", repr(f"{MY_DRIVE}/{src}")).replace("__OUT__", repr(f"{MY_DRIVE}/{out}"))
    output = exec_code(name, session, code, timeout=3600)
    return {"account": name, "session": session, "pack": parse_tagged(output, "PACK")}


def down(account: str, session: str, sync: bool = True) -> dict[str, Any]:
    name = account_entry(account)["name"]
    result: dict[str, Any] = {"account": name, "session": session}
    if sync:
        try:
            synced = sync_outputs(name, session, unmount=True)
        except WorkstationError as exc:
            result["sync_error"] = str(exc)[:500]
            result["status"] = "sync_failed_runtime_kept"
            return result
        if synced is not None:
            result["sync"] = synced
    proc = run_cli(name, "stop", "-s", session, timeout=120)
    result["stop"] = ((proc.stdout or "") + (proc.stderr or "")).strip()[-500:]
    drop_session(name, session)
    result["status"] = "stopped"
    return result


# --------------------------------------------------------------------------- idle watcher


def idle_expired(record: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(record["last_used"])
    except (KeyError, TypeError, ValueError):
        return False
    try:
        if record.get("busy_until") and datetime.fromisoformat(record["busy_until"]) > now:
            return False
    except (TypeError, ValueError):
        pass
    minutes = float(record.get("idle_minutes", DEFAULT_IDLE_MINUTES) or 0)
    if minutes <= 0:
        return False  # 0 = never released by us; Colab's own idle reclaim and 24 h cap still apply
    return (now - last).total_seconds() > minutes * 60


def watch(account: str, session: str, poll: float = 60, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Release the runtime once nothing touched it for idle_minutes; exit when the session is gone."""
    name = account_entry(account)["name"]
    while True:
        record = load_state(name)["sessions"].get(session)
        if record is None:
            return {"account": name, "session": session, "status": "gone"}
        if idle_expired(record):
            return {**down(name, session), "reason": "idle"}
        sleep(poll)


def spawn_watcher(account: str, session: str) -> int:
    log = gemini_web.account_config(account).home / "logs" / f"watch-{session}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "watch", "--account", account,
            "--session", session]
    flags = (gemini_web.CREATE_NO_WINDOW | gemini_web.CREATE_NEW_PROCESS_GROUP | gemini_web.DETACHED_PROCESS
             if os.name == "nt" else 0)
    with open(log, "ab") as out:
        try:
            # Outlive the caller's shell / job object, like the bridge's Chrome.
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=flags | (gemini_web.CREATE_BREAKAWAY_FROM_JOB if flags else 0),
                                    close_fds=True)
        except OSError:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=flags, close_fds=True)
    return proc.pid


# --------------------------------------------------------------------------- CLI


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install-cli", help="Install google-colab-cli into a private venv")
    p.add_argument("--upgrade", action="store_true")

    p = sub.add_parser("status", help="Accounts, bridges, CLI login, compute units and live sessions")
    p.add_argument("--account", action="append")
    p.add_argument("--no-usage", action="store_true")

    p = sub.add_parser("onboard", help="Register an account (1-5) and open its bridge for the one-time sign-in")
    p.add_argument("--account", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--auto-consent", action="store_true",
                   help="Let the bridge click Google's OAuth consent screens (CLI login, Drive) for this account")

    p = sub.add_parser("login", help="One-time colab-cli OAuth, completed in the account's bridge")
    p.add_argument("--account", required=True)

    p = sub.add_parser("up", help="New runtime, Drive mount if needed, stage the profile")
    p.add_argument("--account", required=True, help="Account name, or 'auto' for the most compute units")
    p.add_argument("--profile", required=True)
    p.add_argument("--gpu", choices=GPUS)
    p.add_argument("--high-mem", action="store_true")
    p.add_argument("--session")
    p.add_argument("--idle-minutes", type=int, default=DEFAULT_IDLE_MINUTES)
    p.add_argument("--no-watch", action="store_true")

    p = sub.add_parser("run", help="Execute a .py or .ipynb on the runtime, then copy new outputs to Drive")
    p.add_argument("--account", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("-f", "--file", required=True)
    p.add_argument("--timeout", type=float, default=3600, help="Wall-clock limit in seconds")
    p.add_argument("--no-sync", action="store_true", help="Leave outputs on the VM until `down`")

    p = sub.add_parser("fetch", help="Optional: download one file from the runtime to this machine")
    p.add_argument("--account", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("remote")
    p.add_argument("local")

    p = sub.add_parser("pack", help="Tar a Drive folder of small files into one Drive archive")
    p.add_argument("--account", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--src", required=True, help="Folder under My Drive")
    p.add_argument("--out", required=True, help="Archive path under My Drive")

    p = sub.add_parser("down", help="Copy outputs to Drive, flush, release the runtime")
    p.add_argument("--account", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--no-sync", action="store_true")

    p = sub.add_parser("watch", help=argparse.SUPPRESS)
    p.add_argument("--account", required=True)
    p.add_argument("--session", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "install-cli":
            result = install_cli(args.upgrade)
        elif args.command == "status":
            result = status(args.account, usage=not args.no_usage)
        elif args.command == "onboard":
            result = onboard(args.account, args.email, args.auto_consent)
        elif args.command == "login":
            result = login(args.account)
        elif args.command == "up":
            result = up(args.account, args.profile, gpu=args.gpu, session=args.session,
                        idle_minutes=args.idle_minutes, high_mem=args.high_mem, watch=not args.no_watch)
        elif args.command == "run":
            result = run(args.account, args.session, args.file, timeout=args.timeout, sync=not args.no_sync)
        elif args.command == "fetch":
            result = fetch(args.account, args.session, args.remote, args.local)
        elif args.command == "pack":
            result = pack(args.account, args.session, args.src, args.out)
        elif args.command == "down":
            result = down(args.account, args.session, sync=not args.no_sync)
        else:
            result = watch(args.account, args.session)
    except (WorkstationError, gemini_web.BridgeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in (None, *OK_STATUSES) else 2


if __name__ == "__main__":
    sys.exit(main())
