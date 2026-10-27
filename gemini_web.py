"""Route Manager's Gemini Web bridge: one persistent, signed-in browser for Gemini, AI Studio and Flow.

Modelled on the ChatGPT Web bridge (an isolated browser profile that keeps its login, started and
shown from the Route Manager tray, driven by automation over loopback CDP), but its browser is the
installed Google Chrome on a dedicated profile. Google refuses to sign in inside Electron, which
the ChatGPT Web launcher uses, with "此浏览器或应用可能不安全" ("this browser or app may not be
secure"), even with a Chrome user agent. Chrome on its own --user-data-dir accepts sign-in and
allows a remote-debugging port, which it refuses for the everyday default profile.

    python -X utf8 gemini_web.py status
    python -X utf8 gemini_web.py start [--show] [--site gemini|aistudio|flow]
    python -X utf8 gemini_web.py show [gemini|aistudio|flow]
    python -X utf8 gemini_web.py stop

Automation (omni.py next to this file, and other Gemini tools):

    import gemini_web
    with sync_playwright() as p:
        browser, page = gemini_web.new_task_page(p, "https://gemini.google.com/app")
        ...
        page.close()          # never browser.close(): the bridge outlives the task

Downloads go straight to ~/.gemini-web-bridge/downloads with no Save As prompt (profile prefs).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CHROME = Path(os.environ.get("GEMINI_WEB_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
DATA_DIR = Path(os.environ.get("GEMINI_WEB_DATA_DIR",
                               Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Gemini Web Bridge"))
PROFILE = DATA_DIR / "chrome-profile"
HOME = Path(os.environ.get("GEMINI_WEB_HOME", Path.home() / ".gemini-web-bridge"))
DESCRIPTOR = HOME / "runtime" / "browser.json"
DOWNLOADS = HOME / "downloads"
LOG = Path.home() / ".route-manager" / "gemini-web.log"
CDP_PORT = int(os.environ.get("GEMINI_WEB_CDP_PORT", "17849"))
CDP_ENDPOINT = f"http://127.0.0.1:{CDP_PORT}"
SITES = {
    "gemini": "https://gemini.google.com/app",
    "aistudio": "https://aistudio.google.com/",
    "flow": "https://labs.google/fx/tools/flow",
}
START_WAIT_SECONDS = 30
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class BridgeError(RuntimeError):
    pass


def chrome_args(urls: list[str]) -> list[str]:
    return [
        str(CHROME),
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-name=Gemini Web",
        # Task windows behind other windows must keep rendering (screenshots, video playback, timers).
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        *urls,
    ]


def _is_ours(arguments: list[str]) -> bool:
    profile = f"--user-data-dir={PROFILE}".lower()
    return any(part.lower() == profile for part in arguments[1:])


def _is_our_browser_process(pid: int) -> bool:
    import psutil

    try:
        arguments = psutil.Process(pid).cmdline()
    except psutil.Error:
        return False
    return _is_ours(arguments) and not any(part.startswith("--type=") for part in arguments[1:])


def _chrome_pids() -> list[int]:
    """Filter by image name in Windows first; enumerating every process's details takes seconds."""
    import csv
    import io

    result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, errors="replace", timeout=10,
                            creationflags=CREATE_NO_WINDOW)
    return [int(row[1]) for row in csv.reader(io.StringIO(result.stdout))
            if len(row) > 1 and row[0].lower() == "chrome.exe" and row[1].isdigit()]


def main_pid() -> int | None:
    """Chrome's browser process for the bridge profile (the user's own Chrome uses another profile)."""
    owner = _port_owner()
    if owner and _is_our_browser_process(owner):
        return owner
    # The port is not open yet (still starting) or belongs to someone else.
    return next((pid for pid in _chrome_pids() if _is_our_browser_process(pid)), None)


def _get_json(path: str, method: str = "GET", timeout: float = 3) -> Any:
    request = urllib.request.Request(CDP_ENDPOINT + path, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def cdp_version() -> dict[str, Any] | None:
    try:
        data = _get_json("/json/version")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) else None


def _port_owner() -> int | None:
    import psutil

    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.Error:
        return None
    for connection in connections:
        if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == CDP_PORT:
            return connection.pid
    return None


def health() -> dict[str, Any] | None:
    """Live state, only if the CDP port really belongs to the bridge's Chrome."""
    pid = _port_owner()
    if not pid or not _is_our_browser_process(pid):
        return None
    version = cdp_version()
    if not version:
        return None
    try:
        pages = [t for t in _get_json("/json/list") if t.get("type") == "page"]
    except (OSError, ValueError, urllib.error.URLError):
        pages = []
    return {
        "service": "gemini-web-bridge",
        "pid": pid,
        "browser": version.get("Browser"),
        "cdp": CDP_ENDPOINT,
        "profile": str(PROFILE),
        "downloads": str(DOWNLOADS),
        "pages": [{"title": t.get("title", "")[:80], "url": t.get("url", "")[:160]} for t in pages],
    }


def _prepare_profile() -> None:
    """Point downloads at the bridge folder with no prompt, so no OS Save As dialog ever opens."""
    preferences = PROFILE / "Default" / "Preferences"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(preferences.read_text(encoding="utf-8")) if preferences.is_file() else {}
    except (OSError, ValueError):
        return  # Never clobber a profile we cannot read.
    download = data.setdefault("download", {})
    wanted = {"default_directory": str(DOWNLOADS), "prompt_for_download": False, "directory_upgrade": True}
    if all(download.get(k) == v for k, v in wanted.items()):
        return
    download.update(wanted)
    data.setdefault("savefile", {})["default_directory"] = str(DOWNLOADS)
    part = preferences.with_suffix(".bridge-part")
    part.write_text(json.dumps(data), encoding="utf-8")
    os.replace(part, preferences)


def _write_descriptor(state: dict[str, Any]) -> None:
    DESCRIPTOR.parent.mkdir(parents=True, exist_ok=True)
    part = DESCRIPTOR.with_suffix(".part")
    part.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(part, DESCRIPTOR)


def _launch(urls: list[str]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG, "ab", buffering=0)
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
    try:
        # Break away from the caller's job object so an agent's shell ending never takes Chrome with it.
        subprocess.Popen(chrome_args(urls), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        subprocess.Popen(chrome_args(urls), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)


def start(show: bool = False, site: str = "gemini") -> str:
    """Start the bridge's Chrome if it is down; with `show`, bring a Gemini tab to the front."""
    if not CHROME.is_file():
        raise BridgeError(f"Google Chrome not found: {CHROME}")
    if health():
        if show:
            open_site(site)
            return "已打开 Gemini Web 窗口"
        return "Gemini Web 已在运行"
    if main_pid() is not None:
        return "Gemini Web 正在启动"
    _prepare_profile()
    _launch([SITES.get(site, SITES["gemini"])])
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        live = health()
        if live:
            _write_descriptor({"kind": "gemini-web-bridge", "pid": live["pid"], "cdp": CDP_ENDPOINT,
                               "profile": str(PROFILE), "downloads": str(DOWNLOADS),
                               "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            return "已打开 Gemini Web 窗口" if show else "已启动 Gemini Web"
        time.sleep(0.5)
    raise BridgeError(f"Gemini Web did not open its debugging port within {START_WAIT_SECONDS}s; see {LOG}")


def ensure_running() -> str:
    start(show=False)
    return CDP_ENDPOINT


def open_site(site: str = "gemini") -> dict[str, Any]:
    """Open a site in a new foreground tab of the bridge window (CDP /json/new, then activate)."""
    url = SITES.get(site, site if str(site).startswith("https://") else SITES["gemini"])
    target = _get_json("/json/new?" + urllib.parse.quote(url, safe=":/?=&%"), method="PUT", timeout=10)
    # /json/activate answers with plain text, not JSON.
    urllib.request.urlopen(CDP_ENDPOINT + f"/json/activate/{target['id']}", timeout=5).read()
    return {"id": target.get("id"), "url": url}


def stop(wait: float = 10.0) -> str:
    import psutil

    pid = main_pid()
    if pid is None:
        return "Gemini Web 未在运行"
    # Without /F, taskkill asks Chrome's windows to close, so the profile shuts down cleanly.
    subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and psutil.pid_exists(pid):
        time.sleep(0.3)
    forced = psutil.pid_exists(pid)
    if forced:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    try:
        DESCRIPTOR.unlink()
    except OSError:
        pass
    return "已停止 Gemini Web（强制结束）" if forced else "已停止 Gemini Web"


def connect(playwright: Any) -> tuple[Any, Any]:
    """Attach Playwright to the bridge (starting it if needed); returns (browser, persistent context)."""
    browser = playwright.chromium.connect_over_cdp(ensure_running())
    if not browser.contexts:
        raise BridgeError("Gemini Web has no default browser context")
    return browser, browser.contexts[0]


def new_task_page(playwright: Any, url: str, timeout_ms: int = 60000) -> tuple[Any, Any]:
    """A fresh tab in the signed-in profile, navigated to `url`. Close the page, never the browser."""
    browser, context = connect(playwright)
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    return browser, page


def needs_login(page: Any) -> bool:
    url = page.url.lower()
    return "accounts.google.com" in url or "/signin" in url or "servicelogin" in url


def bring_to_front(page: Any) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "status"
    try:
        if command == "status":
            live = health()
            print(json.dumps({"running": bool(live), **(live or {})}, ensure_ascii=False, indent=2))
            return 0 if live else 1
        if command == "start":
            site = argv[argv.index("--site") + 1] if "--site" in argv else "gemini"
            print(start(show="--show" in argv, site=site))
            return 0
        if command == "show":
            print(start(show=True, site=argv[1] if len(argv) > 1 else "gemini"))
            return 0
        if command == "stop":
            print(stop())
            return 0
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
