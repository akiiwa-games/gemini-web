#!/usr/bin/env python3
"""Dual-Ultra Colab + Google Drive workstation controller on top of the Gemini Web bridge.

Two Google AI Ultra accounts (ultra_a, ultra_b) each get their own Chrome bridge (gemini_web.py
--account) and share /MyDrive/AI_Workstation through a Drive shortcut. A bootstrap notebook in that
folder mounts Drive, stages caches onto the Colab VM's local disk, starts the model server, exposes
it through a tunnel and heartbeats into AI_Workstation/registry/<account>.json.

  python -X utf8 colab_workstation.py status [--account ACCT]
  python -X utf8 colab_workstation.py onboard --account ACCT
  python -X utf8 colab_workstation.py init-bootstrap --out DIR [--server-cmd CMD] [--server-port N] [--tunnel cloudflared|custom|none]
  python -X utf8 colab_workstation.py launch --account ACCT --notebook-url URL [--out DIR] [--prepare-only]
  python -X utf8 colab_workstation.py handoff --from-account A --to-account B --notebook-url URL [--out DIR] [--registry-dir DIR]

Consent boundary (same as omni.py): sign-in pages, Google security challenges, CAPTCHAs, OAuth
consent popups, Colab's "Permit this notebook to access your Google Drive files?" and "not authored by
Google" warnings are never clicked. The run brings the tab to the front, writes login_required or
consent_required to <out>/state.json and stops; the account owner decides, then re-runs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable

import gemini_web

DEFAULT_ACCOUNTS = ("ultra_a", "ultra_b")
DRIVE_FOLDER = "AI_Workstation"
DRIVE_ROOT = f"/content/drive/MyDrive/{DRIVE_FOLDER}"
COLAB_HOST = "colab.research.google.com"
DEFAULT_SERVER_CMD = "python -m http.server {port} --bind 127.0.0.1"
DEFAULT_SERVER_PORT = 8000
TUNNELS = ("cloudflared", "custom", "none")
OBSERVE_SECONDS = 60
STATUSES = ("login_required", "consent_required", "unexpected_page", "prepared_in_browser", "launched")

# Identity checks Google runs before it trusts the session. The owner signs in / verifies; never automated.
SECURITY_CHALLENGE_TEXTS = (
    "Verify it's you",
    "Verify that it's you",
    "This browser or app may not be secure",
    "Couldn't sign you in",
)
CAPTCHA_TEXTS = (
    "I'm not a robot",
    "unusual traffic",
    "Confirm you're not a robot",
    "Are you still there?",
)
# Account decisions shown inside Colab. Each needs the owner's explicit click.
CONSENT_TEXTS = (
    ("Permit this notebook to access your Google Drive files?", "drive_mount_consent"),
    ("Connect to Google Drive", "drive_mount_consent"),
    ("This notebook was not authored by Google", "untrusted_notebook"),
    ("Warning: This notebook was not authored by Google", "untrusted_notebook"),
    ("wants to access your Google Account", "oauth_consent"),
    ("Sign in with Google", "oauth_consent"),
)
SIGNED_OUT_SELECTORS = ('a[href*="ServiceLogin"]', 'a[href*="accounts.google.com/signin"]')
OAUTH_URL_MARKERS = ("accounts.google.com/o/oauth2", "accounts.google.com/signin/oauth",
                     "accounts.google.com/v3/signin", "/signin/oauth/consent")
CHALLENGE_URL_MARKERS = ("/signin/challenge", "/signin/v2/challenge", "google.com/sorry", "/recaptcha/")
GPU_PATTERN = re.compile(r"\b(T4|L4|A100|H100|V100|P100|TPU(?:\s?v\d\w*)?)\b")
COMPUTE_UNITS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s+compute units", re.IGNORECASE)

# Colab's runtime toolbar lives in the <colab-connect-button> shadow root.
RUNTIME_PROBE_JS = r"""() => {
  const button = document.querySelector('colab-connect-button');
  const root = button && (button.shadowRoot || button);
  const text = root ? (root.innerText || root.textContent || '').trim() : '';
  const tip = root ? Array.from(root.querySelectorAll('[tooltiptext],[title],[aria-label]'))
      .map(e => e.getAttribute('tooltiptext') || e.getAttribute('title') || e.getAttribute('aria-label'))
      .filter(Boolean).join(' | ') : '';
  const body = (document.body && document.body.innerText) || '';
  const units = body.match(/\d+(?:\.\d+)?\s+compute units[^\n]*/i);
  return {present: !!button, text: text.slice(0, 300), tooltip: tip.slice(0, 500),
          compute_units_text: units ? units[0].slice(0, 160) : null};
}"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def configured_accounts() -> list[str]:
    raw = os.environ.get("COLAB_WORKSTATION_ACCOUNTS", "")
    names = [part.strip() for part in raw.split(",") if part.strip()] or list(DEFAULT_ACCOUNTS)
    return [gemini_web.normalize_account(name) for name in names]


def default_run_dir(account: str, kind: str = "colab-runs") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return gemini_web.account_config(account).home / kind / stamp


# --------------------------------------------------------------------------- status / onboard


def account_status(account: str) -> dict[str, Any]:
    cfg = gemini_web.account_config(account)
    live = gemini_web.health(account=cfg.name)
    tabs = [page for page in (live or {}).get("pages", []) if COLAB_HOST in page.get("url", "")]
    return {"account": cfg.name, "running": bool(live), "cdp": cfg.endpoint, "profile": str(cfg.profile),
            "pid": (live or {}).get("pid"), "colab_tabs": tabs}


def status(accounts: list[str] | None = None) -> dict[str, Any]:
    names = accounts or configured_accounts()
    return {"checked_at": utc_now(), "drive_root": DRIVE_ROOT, "chrome": str(gemini_web.CHROME),
            "chrome_found": gemini_web.CHROME.is_file(), "accounts": [account_status(name) for name in names]}


def onboard(account: str) -> dict[str, Any]:
    """Show the account's own Chrome with Drive and Colab tabs; the owner signs in and adds the shortcut."""
    name = gemini_web.normalize_account(account)
    if name == gemini_web.DEFAULT_ACCOUNT:
        raise gemini_web.BridgeError("onboard needs a named account (e.g. ultra_a), not the default bridge")
    message = gemini_web.start(show=True, site="drive", account=name)
    colab = gemini_web.open_site("colab", account=name)
    return {"account": name, "message": message, "colab_tab": colab, "next_steps": [
        "Sign in to this account's Google profile in the window that just opened (once; the profile keeps it).",
        f"In Google Drive: right-click the shared '{DRIVE_FOLDER}' folder > Organize > Add shortcut > My Drive.",
        f"Optionally create '/MyDrive/.ai_workstation_account' containing '{name}' so the bootstrap knows who it runs as.",
        "Open bootstrap_workstation.ipynb from the shortcut in Colab and approve Drive access once.",
    ]}


# --------------------------------------------------------------------------- bootstrap generator

BOOTSTRAP_PY = r'''"""AI Workstation bootstrap: mount Drive, stage caches to local disk, start the server, tunnel, heartbeat.

Runs inside a Colab notebook cell (exec'd by bootstrap_workstation.ipynb) or as
`python workstation_bootstrap.py --account ultra_a` on a VM where Drive is already mounted.
"""
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from datetime import datetime, timezone

DRIVE_MOUNT = "/content/drive"
WORKSTATION = "/content/drive/MyDrive/__DRIVE_FOLDER__"
LOCAL_CACHE = "/content/local_cache"
ACCOUNT_MARKER = DRIVE_MOUNT + "/MyDrive/.ai_workstation_account"
CLOUDFLARED = "/usr/local/bin/cloudflared"
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
TUNNEL_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def mount_drive(mount=DRIVE_MOUNT):
    """drive.mount shows Colab's consent dialog; the account owner approves it in the browser."""
    if os.path.isdir(mount + "/MyDrive"):
        return mount
    from google.colab import drive  # only inside Colab
    drive.mount(mount)
    return mount


def verify_workstation(root=WORKSTATION):
    if not os.path.isdir(root):
        raise SystemExit(f"{root} not found: add the shared AI_Workstation folder to My Drive as a shortcut")
    for sub in ("registry", "env", "models", "logs"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def resolve_account(account="auto"):
    if account and account != "auto":
        return account
    if os.environ.get("WORKSTATION_ACCOUNT"):
        return os.environ["WORKSTATION_ACCOUNT"]
    try:
        with open(ACCOUNT_MARKER, encoding="utf-8") as handle:
            return handle.read().strip() or "unknown"
    except OSError:
        return "unknown"


def stage_local_cache(root=WORKSTATION, cache=LOCAL_CACHE):
    """Drive FUSE is slow for many small files: unpack archives once onto the VM's local NVMe."""
    os.makedirs(cache, exist_ok=True)
    staged = []
    for archive in sorted(glob.glob(os.path.join(root, "env", "*"))):
        name = os.path.basename(archive)
        target = os.path.join(cache, re.sub(r"(\.tar\.gz|\.tgz|\.tar|\.zip)$", "", name))
        if os.path.exists(target + ".staged"):
            staged.append(target)
            continue
        if name.endswith((".tar.gz", ".tgz", ".tar")):
            local = os.path.join(cache, name)
            shutil.copy(archive, local)  # one large sequential read beats extracting over FUSE
            with tarfile.open(local) as tar:
                tar.extractall(target)
            os.remove(local)
        elif name.endswith(".zip"):
            local = os.path.join(cache, name)
            shutil.copy(archive, local)
            with zipfile.ZipFile(local) as zf:
                zf.extractall(target)
            os.remove(local)
        else:
            continue
        open(target + ".staged", "w").close()
        staged.append(target)
    hf_home = os.path.join(cache, "hf")
    os.makedirs(hf_home, exist_ok=True)
    os.environ["HF_HOME"] = hf_home
    os.environ["HF_HUB_CACHE"] = os.path.join(hf_home, "hub")
    models_local = os.path.join(cache, "models")
    os.makedirs(models_local, exist_ok=True)
    for src in sorted(glob.glob(os.path.join(root, "models", "*"))):
        dst = os.path.join(models_local, os.path.basename(src))
        if not os.path.exists(dst):
            (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, dst)
    if not os.path.lexists("/content/models"):
        os.symlink(models_local, "/content/models")
    return {"staged": staged, "hf_home": hf_home, "models": models_local}


def gpu_name():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        return out.splitlines()[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return "TPU" if os.environ.get("COLAB_TPU_ADDR") or os.environ.get("TPU_NAME") else None


def start_server(command, port, log_dir):
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "server.log"), "ab")
    proc = subprocess.Popen(command.format(port=port), shell=True, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 600
    while time.time() < deadline and proc.poll() is None:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", int(port))) == 0:
                return proc
        time.sleep(2)
    return proc


def start_tunnel(kind, port, custom_cmd="", custom_url=""):
    if kind == "none":
        return None, None
    if kind == "custom":
        proc = subprocess.Popen(custom_cmd.format(port=port), shell=True) if custom_cmd else None
        return proc, custom_url or None
    if not os.path.exists(CLOUDFLARED):
        subprocess.run(["wget", "-q", "-O", CLOUDFLARED, CLOUDFLARED_URL], check=True)
        os.chmod(CLOUDFLARED, 0o755)
    proc = subprocess.Popen([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 90
    while time.time() < deadline:
        line = proc.stdout.readline()
        match = TUNNEL_URL_PATTERN.search(line or "")
        if match:
            threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
            return proc, match.group(0)
    return proc, None


def write_registry(root, account, record):
    path = os.path.join(root, "registry", f"{account}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def heartbeat(root, account, record, server, interval=60, stop=None):
    stop = stop or threading.Event()

    def beat():
        while not stop.is_set():
            record["updated_at"] = utc_now()
            record["status"] = "running" if server is None or server.poll() is None else "server_exited"
            write_registry(root, account, record)
            if record["status"] != "running":
                return
            stop.wait(interval)

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    return thread, stop


def bootstrap(account="auto", server_cmd=__SERVER_CMD__, server_port=__SERVER_PORT__, tunnel="__TUNNEL__",
              tunnel_cmd="", tunnel_url="", heartbeat_seconds=60, mount=True):
    if mount:
        mount_drive()
    root = verify_workstation()
    account = resolve_account(account)
    record = {"account": account, "started_at": utc_now(), "updated_at": utc_now(), "gpu_name": gpu_name(),
              "server_port": int(server_port), "server_cmd": server_cmd, "tunnel": tunnel, "tunnel_url": None,
              "host": socket.gethostname(), "status": "starting"}
    write_registry(root, account, record)
    record["cache"] = stage_local_cache(root)
    server = start_server(server_cmd, server_port, os.path.join(root, "logs", account))
    if server.poll() is not None:
        record["status"] = "server_exited"
        write_registry(root, account, record)
        raise SystemExit(f"server exited with {server.returncode}; see {root}/logs/{account}/server.log")
    _, record["tunnel_url"] = start_tunnel(tunnel, server_port, tunnel_cmd, tunnel_url)
    record["status"] = "running"
    write_registry(root, account, record)
    heartbeat(root, account, record, server, heartbeat_seconds)
    return record


if __name__ == "__main__" and "ipykernel" not in sys.modules:  # a notebook cell only defines the helpers
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="auto")
    parser.add_argument("--server-cmd", default=__SERVER_CMD__)
    parser.add_argument("--server-port", type=int, default=__SERVER_PORT__)
    parser.add_argument("--tunnel", default="__TUNNEL__", choices=("cloudflared", "custom", "none"))
    parser.add_argument("--tunnel-cmd", default="")
    parser.add_argument("--tunnel-url", default="")
    parser.add_argument("--no-mount", action="store_true")
    args = parser.parse_args()
    rec = bootstrap(args.account, args.server_cmd, args.server_port, args.tunnel, args.tunnel_cmd,
                    args.tunnel_url, mount=not args.no_mount)
    print(json.dumps(rec, indent=2))
    while True:  # keep the server and heartbeat alive in a plain shell
        time.sleep(3600)
'''

BOOTSTRAP_SH = """#!/usr/bin/env bash
# Run on a Colab terminal / VM where Google Drive is already mounted at /content/drive.
set -euo pipefail
ROOT=/content/drive/MyDrive/__DRIVE_FOLDER__
python3 "$ROOT/workstation_bootstrap.py" --no-mount "$@"
"""


def _render(template: str, server_cmd: str, server_port: int, tunnel: str) -> str:
    return (template.replace("__DRIVE_FOLDER__", DRIVE_FOLDER)
            .replace("__SERVER_CMD__", json.dumps(server_cmd))
            .replace("__SERVER_PORT__", str(int(server_port)))
            .replace("__TUNNEL__", tunnel))


def _cell(kind: str, source: str, **extra: Any) -> dict[str, Any]:
    lines = source.splitlines(keepends=True)
    cell: dict[str, Any] = {"cell_type": kind, "metadata": extra.pop("metadata", {}), "source": lines}
    if kind == "code":
        cell.update({"execution_count": None, "outputs": []})
    return cell


def build_notebook(server_cmd: str = DEFAULT_SERVER_CMD, server_port: int = DEFAULT_SERVER_PORT,
                   tunnel: str = "cloudflared") -> dict[str, Any]:
    if tunnel not in TUNNELS:
        raise ValueError(f"Unsupported tunnel {tunnel!r}. Allowed: {TUNNELS}")
    helper = _render(BOOTSTRAP_PY, server_cmd, server_port, tunnel)
    params = "\n".join([
        "# @title Workstation parameters",
        'ACCOUNT = "auto"  # @param {type:"string"}',
        f"SERVER_CMD = {json.dumps(server_cmd)}  # @param {{type:\"string\"}}",
        f"SERVER_PORT = {int(server_port)}  # @param {{type:\"integer\"}}",
        f"TUNNEL = {json.dumps(tunnel)}  # @param [\"cloudflared\", \"custom\", \"none\"]",
        'TUNNEL_CMD = ""  # @param {type:"string"}',
        'TUNNEL_URL = ""  # @param {type:"string"}',
        "HEARTBEAT_SECONDS = 60  # @param {type:\"integer\"}",
    ]) + "\n"
    run = ("record = bootstrap(ACCOUNT, SERVER_CMD, SERVER_PORT, TUNNEL, TUNNEL_CMD, TUNNEL_URL, HEARTBEAT_SECONDS)\n"
           "print(json.dumps(record, indent=2))\n")
    keepalive = ("# Keeps the kernel busy so the server, tunnel and heartbeat threads stay up.\n"
                 "import time\nwhile True:\n    time.sleep(300)\n")
    intro = (f"# AI Workstation bootstrap\n\nMounts Google Drive, verifies `{DRIVE_ROOT}`, stages "
             "`env/` archives and `models/` onto `/content/local_cache` (sets `HF_HOME`), starts the model "
             "server, exposes it through the selected tunnel and heartbeats "
             f"`{DRIVE_ROOT}/registry/<account>.json` (`account`, `started_at`, `gpu_name`, `tunnel_url`, `status`).\n\n"
             "Run all (Ctrl+F9). Approve the Drive permission dialog yourself the first time.\n")
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "accelerator": "GPU",
            "colab": {"name": "bootstrap_workstation.ipynb", "provenance": [], "gpuType": "T4"},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "cells": [
            _cell("markdown", intro, metadata={"id": "intro"}),
            _cell("code", params, metadata={"id": "params", "cellView": "form"}),
            _cell("code", helper, metadata={"id": "helper"}),
            _cell("code", run, metadata={"id": "run"}),
            _cell("code", keepalive, metadata={"id": "keepalive"}),
        ],
    }


def init_bootstrap(out: str | Path, server_cmd: str = DEFAULT_SERVER_CMD, server_port: int = DEFAULT_SERVER_PORT,
                   tunnel: str = "cloudflared", overwrite: bool = False) -> dict[str, Any]:
    out = Path(out).expanduser().resolve()
    notebook = build_notebook(server_cmd, server_port, tunnel)
    files = {
        "bootstrap_workstation.ipynb": json.dumps(notebook, ensure_ascii=False, indent=1) + "\n",
        "workstation_bootstrap.py": _render(BOOTSTRAP_PY, server_cmd, server_port, tunnel),
        "workstation_bootstrap.sh": _render(BOOTSTRAP_SH, server_cmd, server_port, tunnel),
    }
    existing = [name for name in files if (out / name).exists()]
    if existing and not overwrite:
        raise FileExistsError(f"Refusing to overwrite {existing} in {out} (pass --force)")
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    return {"out": str(out), "files": sorted(files), "upload_to": f"/MyDrive/{DRIVE_FOLDER}/",
            "server_cmd": server_cmd, "server_port": int(server_port), "tunnel": tunnel}


# --------------------------------------------------------------------------- browser launch


def _text_visible(page: Any, text: str) -> bool:
    try:
        return page.get_by_text(text).count() > 0
    except Exception:
        return False


def _selector_present(page: Any, selector: str) -> bool:
    try:
        return page.locator(selector).count() > 0
    except Exception:
        return False


def _context_pages(page: Any) -> list[Any]:
    try:
        return list(page.context.pages)
    except Exception:
        return [page]


def detect_blocker(page: Any) -> dict[str, Any] | None:
    """A page (or popup) waiting for the account owner. Detection only: nothing here clicks."""
    for candidate in _context_pages(page) or [page]:
        url = (getattr(candidate, "url", "") or "").lower()
        if any(marker in url for marker in CHALLENGE_URL_MARKERS):
            reason = "captcha" if ("sorry" in url or "recaptcha" in url) else "security_challenge"
            return {"status": "login_required" if reason == "security_challenge" else "consent_required",
                    "reason": reason, "observed_url": candidate.url, "page": candidate}
        if any(marker in url for marker in OAUTH_URL_MARKERS):
            return {"status": "consent_required", "reason": "oauth_consent", "observed_url": candidate.url,
                    "page": candidate}
    if gemini_web.needs_login(page):
        return {"status": "login_required", "reason": "signed_out", "observed_url": page.url, "page": page}
    for text in SECURITY_CHALLENGE_TEXTS:
        if _text_visible(page, text):
            return {"status": "login_required", "reason": "security_challenge", "matched": text,
                    "observed_url": page.url, "page": page}
    for text in CAPTCHA_TEXTS:
        if _text_visible(page, text):
            return {"status": "consent_required", "reason": "captcha", "matched": text,
                    "observed_url": page.url, "page": page}
    for text, reason in CONSENT_TEXTS:
        if _text_visible(page, text):
            return {"status": "consent_required", "reason": reason, "matched": text,
                    "observed_url": page.url, "page": page}
    if any(_selector_present(page, selector) for selector in SIGNED_OUT_SELECTORS):
        return {"status": "login_required", "reason": "colab_signed_out", "observed_url": page.url, "page": page}
    return None


def inspect_runtime(page: Any) -> dict[str, Any]:
    try:
        probe = page.evaluate(RUNTIME_PROBE_JS) or {}
    except Exception as exc:
        probe = {"present": False, "error": str(exc)[:200]}
    combined = " ".join(str(probe.get(key) or "") for key in ("text", "tooltip"))
    lowered = combined.lower()
    connected = bool(probe.get("present")) and (
        "ram" in lowered or "disk" in lowered or "connected to" in lowered) and "reconnect" not in lowered
    gpu = GPU_PATTERN.search(combined)
    units = COMPUTE_UNITS_PATTERN.search(str(probe.get("compute_units_text") or ""))
    return {"toolbar_present": bool(probe.get("present")), "connected": connected,
            "toolbar_text": probe.get("text"), "tooltip": probe.get("tooltip"),
            "gpu": gpu.group(1) if gpu else None,
            "compute_units": float(units.group(1)) if units else None,
            "compute_units_text": probe.get("compute_units_text")}


def _snapshot(page: Any, out_dir: Path, name: str) -> str | None:
    try:
        path = out_dir / name
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return None


def run_all(page: Any) -> None:
    """Colab's Runtime > Run all. Connecting happens as part of it; no dialog is clicked."""
    try:
        page.locator("body").first.click(position={"x": 5, "y": 5}, timeout=3000)
    except Exception:
        pass
    page.keyboard.press("Control+F9")


def run_page(page: Any, account: str, notebook_url: str, out_dir: Path, *, prepare_only: bool = False,
             observe_seconds: int = OBSERVE_SECONDS, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    base = {"account": account, "notebook_url": notebook_url, "observed_url": page.url, "checked_at": utc_now()}

    def blocked(blocker: dict[str, Any], stage: str) -> dict[str, Any]:
        target = blocker.pop("page", page)
        gemini_web.bring_to_front(target)
        return {**base, **blocker, "stage": stage, "runtime": inspect_runtime(page),
                "screenshot": _snapshot(target, out_dir, "blocked.png"),
                "next_step": "The account owner must finish this step in the browser window, then re-run."}

    blocker = detect_blocker(page)
    if blocker:
        return blocked(blocker, "before_run")
    if COLAB_HOST not in page.url.lower():
        gemini_web.bring_to_front(page)
        return {**base, "status": "unexpected_page", "runtime": inspect_runtime(page),
                "next_step": "The URL did not land on Colab; check the notebook link."}
    runtime = inspect_runtime(page)
    if prepare_only:
        gemini_web.bring_to_front(page)
        return {**base, "status": "prepared_in_browser", "runtime": runtime,
                "screenshot": _snapshot(page, out_dir, "prepared.png")}
    run_all(page)
    triggered_at = utc_now()
    deadline = clock() + observe_seconds
    while True:
        blocker = detect_blocker(page)
        if blocker:
            state = blocked(blocker, "after_run_all")
            state["run_all_at"] = triggered_at
            return state
        runtime = inspect_runtime(page)
        if runtime["connected"] or clock() >= deadline:
            break
        sleep(2)
    return {**base, "status": "launched", "run_all_at": triggered_at, "runtime": runtime,
            "observed_url": page.url, "screenshot": _snapshot(page, out_dir, "launched.png")}


def _sync_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def launch(account: str, notebook_url: str, out_dir: str | Path | None = None, *, prepare_only: bool = False,
           observe_seconds: int = OBSERVE_SECONDS) -> dict[str, Any]:
    name = gemini_web.normalize_account(account)
    if not notebook_url.startswith("https://"):
        raise ValueError("--notebook-url must be an https:// Colab URL")
    out = Path(out_dir).expanduser().resolve() if out_dir else default_run_dir(name)
    state_path = out / "state.json"
    atomic_write_json(state_path, {"status": "launching_browser", "account": name, "notebook_url": notebook_url,
                                   "started_at": utc_now()})
    with _sync_playwright() as p:
        _, page = gemini_web.new_task_page(p, notebook_url, account=name)
        try:
            page.wait_for_timeout(3000)  # Colab hydrates its toolbar after domcontentloaded
        except Exception:
            pass
        state = run_page(page, name, notebook_url, out, prepare_only=prepare_only, observe_seconds=observe_seconds)
    state["state_path"] = str(state_path)
    atomic_write_json(state_path, state)
    return state


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def registry_fresh(record: dict[str, Any] | None, since: str) -> bool:
    """The account's heartbeat says running and was written after `since`."""
    if not record or record.get("status") != "running":
        return False
    updated, start = _parse_time(record.get("updated_at")), _parse_time(since)
    return bool(updated and start and updated >= start)


def read_registry(registry_dir: str | Path, account: str) -> dict[str, Any] | None:
    try:
        return json.loads((Path(registry_dir) / f"{account}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def wait_registry_ready(registry_dir: str | Path, account: str, since: str, timeout: float,
                        sleep: Callable[[float], None] = time.sleep,
                        clock: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    """A locally synced registry/<account>.json (Drive for desktop) reporting running after `since`."""
    deadline = clock() + timeout
    while True:
        record = read_registry(registry_dir, account)
        if registry_fresh(record, since):
            return record
        if clock() >= deadline:
            return record
        sleep(10)


def handoff(from_account: str, to_account: str, notebook_url: str, out_dir: str | Path | None = None, *,
            registry_dir: str | Path | None = None, ready_timeout: float = 900,
            observe_seconds: int = OBSERVE_SECONDS,
            launcher: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Bring up `to_account` first; `from_account` keeps running until the owner retires it."""
    source = gemini_web.normalize_account(from_account)
    target = gemini_web.normalize_account(to_account)
    if source == target:
        raise ValueError("--from-account and --to-account must differ")
    out = Path(out_dir).expanduser().resolve() if out_dir else default_run_dir(target, "handoffs")
    started = utc_now()
    state = (launcher or launch)(target, notebook_url, out / target, observe_seconds=observe_seconds)
    receipt: dict[str, Any] = {"from_account": source, "to_account": target, "notebook_url": notebook_url,
                               "started_at": started, "target_state": state, "registry": None}
    ready = state.get("status") == "launched"
    if ready and registry_dir:
        record = wait_registry_ready(registry_dir, target, started, ready_timeout)
        receipt["registry"] = record
        ready = registry_fresh(record, started)
    receipt["ready"] = ready
    receipt["status"] = "handoff_ready" if ready else f"handoff_blocked:{state.get('status')}"
    receipt["next_step"] = (
        f"{target} is serving; stop {source}'s runtime in its Colab tab (Runtime > Disconnect and delete runtime) "
        "when clients have moved to the new endpoint." if ready else
        f"{source} keeps serving. Resolve {target}'s state ({state.get('status')}) and re-run the handoff.")
    receipt["finished_at"] = utc_now()
    atomic_write_json(out / "handoff.json", receipt)
    receipt["receipt_path"] = str(out / "handoff.json")
    return receipt


# --------------------------------------------------------------------------- CLI


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dual-Ultra Colab + Google Drive workstation controller")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("status")
    s.add_argument("--account", action="append")
    o = sub.add_parser("onboard")
    o.add_argument("--account", required=True)
    b = sub.add_parser("init-bootstrap")
    b.add_argument("--out", required=True)
    b.add_argument("--server-cmd", default=DEFAULT_SERVER_CMD, help="'{port}' is replaced with --server-port")
    b.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
    b.add_argument("--tunnel", choices=TUNNELS, default="cloudflared")
    b.add_argument("--force", action="store_true", help="overwrite existing generated files")
    l = sub.add_parser("launch")
    l.add_argument("--account", required=True)
    l.add_argument("--notebook-url", required=True)
    l.add_argument("--out")
    l.add_argument("--prepare-only", action="store_true")
    l.add_argument("--observe-seconds", type=int, default=OBSERVE_SECONDS)
    h = sub.add_parser("handoff")
    h.add_argument("--from-account", required=True)
    h.add_argument("--to-account", required=True)
    h.add_argument("--notebook-url", required=True)
    h.add_argument("--out")
    h.add_argument("--registry-dir", help="locally synced AI_Workstation/registry (Google Drive for desktop)")
    h.add_argument("--ready-timeout", type=float, default=900)
    h.add_argument("--observe-seconds", type=int, default=OBSERVE_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "status":
            result = status(args.account)
            code = 0
        elif args.command == "onboard":
            result = onboard(args.account)
            code = 0
        elif args.command == "init-bootstrap":
            result = init_bootstrap(args.out, args.server_cmd, args.server_port, args.tunnel, overwrite=args.force)
            code = 0
        elif args.command == "launch":
            result = launch(args.account, args.notebook_url, args.out, prepare_only=args.prepare_only,
                            observe_seconds=args.observe_seconds)
            code = 0 if result["status"] in ("launched", "prepared_in_browser") else 3
        else:
            result = handoff(args.from_account, args.to_account, args.notebook_url, args.out,
                             registry_dir=args.registry_dir, ready_timeout=args.ready_timeout,
                             observe_seconds=args.observe_seconds)
            code = 0 if result["ready"] else 3
    except (gemini_web.BridgeError, ValueError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
