"""Route Manager's Gemini Web bridge: one persistent, signed-in browser for Gemini, AI Studio and Flow.

Modelled on the ChatGPT Web bridge (an isolated browser profile that keeps its login, started and
shown from the Route Manager tray, driven by automation over loopback CDP), but its browser is the
installed Google Chrome on a dedicated profile. Google refuses to sign in inside Electron, which
the ChatGPT Web launcher uses, with "此浏览器或应用可能不安全" ("this browser or app may not be
secure"), even with a Chrome user agent. Chrome on its own --user-data-dir accepts sign-in and
allows a remote-debugging port, which it refuses for the everyday default profile.

    python -X utf8 gemini_web.py status [--account NAME]
    python -X utf8 gemini_web.py start [--show] [--site gemini|aistudio|flow|colab|drive] [--account NAME]
    python -X utf8 gemini_web.py show [gemini|aistudio|flow|colab|drive] [--account NAME]
    python -X utf8 gemini_web.py stop [--account NAME]

Without --account (or with --account default) everything is exactly the single-profile bridge.
A named account (letters, digits, '_' and '-') gets its own Chrome profile, runtime folder and
loopback CDP port, so two Google accounts never share cookies, `authuser=0` or a debugging port.

Automation (omni.py next to this file, and other Gemini tools):

    import gemini_web
    with sync_playwright() as p:
        browser, page = gemini_web.new_task_page(p, "https://gemini.google.com/app")
        # or new_task_page(p, url, account="ultra_a") for a named account's Chrome
        ...
        page.close()          # never browser.close(): the bridge outlives the task

Downloads go straight to ~/.gemini-web-bridge/downloads with no Save As prompt (profile prefs).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

def discover_chrome() -> Path:
    """Respect the explicit executable, then discover system, user and PATH installs."""
    override = os.environ.get("GEMINI_WEB_CHROME")
    if override:
        return Path(override).expanduser()
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(key)
        if base:
            candidate = Path(base) / "Google/Chrome/Application/chrome.exe"
            if candidate.is_file():
                return candidate
    for name in ("chrome", "chrome.exe", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        executable = shutil.which(name)
        if executable:
            return Path(executable)
    return Path("chrome.exe")  # start() reports the missing installation without launching anything.


CHROME = discover_chrome()
DATA_DIR = Path(os.environ.get("GEMINI_WEB_DATA_DIR",
                               Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Gemini Web Bridge"))
PROFILE = DATA_DIR / "chrome-profile"
HOME = Path(os.environ.get("GEMINI_WEB_HOME", Path.home() / ".gemini-web-bridge"))
DESCRIPTOR = HOME / "runtime" / "browser.json"
DOWNLOADS = HOME / "downloads"
LOG = Path.home() / ".route-manager" / "gemini-web.log"
def _cdp_port() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        if str(root) not in sys.path:
            sys.path.append(str(root))
        from agentcompass.ports import get
        return get("gemini_web")
    except Exception:
        return int(os.environ.get("GEMINI_WEB_CDP_PORT", "17849"))


CDP_PORT = _cdp_port()
CDP_ENDPOINT = f"http://127.0.0.1:{CDP_PORT}"
SITES = {
    "gemini": "https://gemini.google.com/app",
    "aistudio": "https://aistudio.google.com/",
    "flow": "https://labs.google/fx/tools/flow",
    "colab": "https://colab.research.google.com/",
    "drive": "https://drive.google.com/drive/my-drive",
}
DEFAULT_ACCOUNT = "default"
ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# First named slots. 17851 is skipped: AgentCompass reserves it for its account-login Chrome.
NAMED_ACCOUNT_PORTS = {"ultra_a": 17850, "ultra_b": 17852}
RESERVED_PORTS = frozenset({17843, 17849, 17851})
HASHED_PORT_BASE = 17860
HASHED_PORT_SPAN = 100
START_WAIT_SECONDS = 30
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountConfig:
    """Everything that must differ between two Google accounts' bridges."""

    name: str
    profile: Path
    home: Path
    descriptor: Path
    downloads: Path
    port: int
    log: Path

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT_ACCOUNT

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def window_name(self) -> str:
        return "Gemini Web" if self.is_default else f"Gemini Web ({self.name})"

    @property
    def label(self) -> str:
        return "Gemini Web" if self.is_default else f"Gemini Web [{self.name}]"


def normalize_account(account: str | None) -> str:
    name = (account or DEFAULT_ACCOUNT).strip() or DEFAULT_ACCOUNT
    if not ACCOUNT_PATTERN.match(name):
        raise BridgeError(f"Invalid account name {account!r}: use letters, digits, '_' or '-' (max 64)")
    return name


def _port_env_key(name: str) -> str:
    return "GEMINI_WEB_CDP_PORT_" + name.upper().replace("-", "_")


def _registered_ports() -> dict[str, int]:
    """Optional <HOME>/accounts.json: {"accounts": {"ultra_a": {"port": 17850}, ...}}."""
    try:
        data = json.loads((HOME / "accounts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        return {}
    ports: dict[str, int] = {}
    for name, entry in accounts.items():
        raw = entry.get("port") if isinstance(entry, dict) else entry
        try:
            port = int(raw)
        except (TypeError, ValueError):
            continue
        if 1024 <= port <= 65535:
            ports[str(name)] = port
    return ports


def account_port(name: str) -> int:
    """Env GEMINI_WEB_CDP_PORT_<NAME>, then <HOME>/accounts.json, then the named slots, then a stable hash."""
    name = normalize_account(name)
    if name == DEFAULT_ACCOUNT:
        return CDP_PORT
    raw = os.environ.get(_port_env_key(name))
    if raw:
        try:
            return int(raw)
        except ValueError:
            raise BridgeError(f"{_port_env_key(name)} is not a port number: {raw!r}") from None
    registered = _registered_ports().get(name)
    if registered:
        return registered
    if name in NAMED_ACCOUNT_PORTS:
        return NAMED_ACCOUNT_PORTS[name]
    taken = RESERVED_PORTS | {CDP_PORT} | set(NAMED_ACCOUNT_PORTS.values())
    offset = int(hashlib.sha256(name.encode("utf-8")).hexdigest(), 16) % HASHED_PORT_SPAN
    for step in range(HASHED_PORT_SPAN):
        port = HASHED_PORT_BASE + (offset + step) % HASHED_PORT_SPAN
        if port not in taken:
            return port
    raise BridgeError("No free hashed CDP port")


def account_config(account: str | None = None) -> AccountConfig:
    """Paths and port for `account`; the default account reads the module constants at call time."""
    name = normalize_account(account)
    if name == DEFAULT_ACCOUNT:
        return AccountConfig(name, PROFILE, HOME, DESCRIPTOR, DOWNLOADS, CDP_PORT, LOG)
    home = HOME / "accounts" / name
    return AccountConfig(
        name=name,
        profile=DATA_DIR / "accounts" / name / "chrome-profile",
        home=home,
        descriptor=home / "runtime" / "browser.json",
        downloads=home / "downloads",
        port=account_port(name),
        log=LOG.with_name(f"gemini-web-{name}.log"),
    )


def _named(cfg: AccountConfig) -> dict[str, str]:
    """Default-account internal calls keep their historical signatures (callers and tests patch them)."""
    return {} if cfg.is_default else {"account": cfg.name}


def chrome_args(urls: list[str], account: str | None = None) -> list[str]:
    cfg = account_config(account)
    return [
        str(CHROME),
        f"--user-data-dir={cfg.profile}",
        f"--remote-debugging-port={cfg.port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-name={cfg.window_name}",
        # Task windows behind other windows must keep rendering (screenshots, video playback, timers).
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        *urls,
    ]


def _is_ours(arguments: list[str], profile: Path | None = None) -> bool:
    wanted = f"--user-data-dir={PROFILE if profile is None else profile}".lower()
    return any(part.lower() == wanted for part in arguments[1:])


def _is_our_browser_process(pid: int, profile: Path | None = None) -> bool:
    import psutil

    try:
        arguments = psutil.Process(pid).cmdline()
    except psutil.Error:
        return False
    return _is_ours(arguments, profile) and not any(part.startswith("--type=") for part in arguments[1:])


def _chrome_pids() -> list[int]:
    """Filter by image name in Windows first; enumerating every process's details takes seconds."""
    import csv
    import io

    result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, errors="replace", timeout=10,
                            creationflags=CREATE_NO_WINDOW)
    return [int(row[1]) for row in csv.reader(io.StringIO(result.stdout))
            if len(row) > 1 and row[0].lower() == "chrome.exe" and row[1].isdigit()]


def _endpoint(cfg: AccountConfig) -> str:
    return CDP_ENDPOINT if cfg.is_default else cfg.endpoint


def _ours(pid: int, cfg: AccountConfig) -> bool:
    return _is_our_browser_process(pid) if cfg.is_default else _is_our_browser_process(pid, cfg.profile)


def _owner(cfg: AccountConfig) -> int | None:
    return _port_owner() if cfg.is_default else _port_owner(cfg.port)


def main_pid(account: str | None = None) -> int | None:
    """Chrome's browser process for the bridge profile (the user's own Chrome uses another profile)."""
    cfg = account_config(account)
    owner = _owner(cfg)
    if owner and _ours(owner, cfg):
        return owner
    # The port is not open yet (still starting) or belongs to someone else.
    return next((pid for pid in _chrome_pids() if _ours(pid, cfg)), None)


def _get_json(path: str, method: str = "GET", timeout: float = 3, endpoint: str | None = None) -> Any:
    request = urllib.request.Request((endpoint or CDP_ENDPOINT) + path, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def cdp_version(endpoint: str | None = None) -> dict[str, Any] | None:
    try:
        data = _get_json("/json/version", endpoint=endpoint)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) else None


def _port_owner(port: int | None = None) -> int | None:
    import psutil

    port = CDP_PORT if port is None else port
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.Error:
        return None
    for connection in connections:
        if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == port:
            return connection.pid
    return None


def health(account: str | None = None) -> dict[str, Any] | None:
    """Live state, only if the CDP port really belongs to the bridge's Chrome."""
    cfg = account_config(account)
    pid = _owner(cfg)
    if not pid or not _ours(pid, cfg):
        return None
    version = cdp_version() if cfg.is_default else cdp_version(cfg.endpoint)
    if not version:
        return None
    try:
        pages = [t for t in _get_json("/json/list", endpoint=_endpoint(cfg)) if t.get("type") == "page"]
    except (OSError, ValueError, urllib.error.URLError):
        pages = []
    state = {
        "service": "gemini-web-bridge",
        "pid": pid,
        "browser": version.get("Browser"),
        "cdp": _endpoint(cfg),
        "profile": str(cfg.profile),
        "downloads": str(cfg.downloads),
        "pages": [{"title": t.get("title", "")[:80], "url": t.get("url", "")[:160]} for t in pages],
    }
    if not cfg.is_default:
        state["account"] = cfg.name
    return state


def _prepare_profile(account: str | None = None) -> None:
    """Point downloads at the bridge folder with no prompt, so no OS Save As dialog ever opens."""
    cfg = account_config(account)
    downloads = cfg.downloads
    preferences = cfg.profile / "Default" / "Preferences"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(preferences.read_text(encoding="utf-8")) if preferences.is_file() else {}
    except (OSError, ValueError):
        return  # Never clobber a profile we cannot read.
    download = data.setdefault("download", {})
    wanted = {"default_directory": str(downloads), "prompt_for_download": False, "directory_upgrade": True}
    if all(download.get(k) == v for k, v in wanted.items()):
        return
    download.update(wanted)
    data.setdefault("savefile", {})["default_directory"] = str(downloads)
    part = preferences.with_suffix(".bridge-part")
    part.write_text(json.dumps(data), encoding="utf-8")
    os.replace(part, preferences)


def _write_descriptor(state: dict[str, Any], account: str | None = None) -> None:
    descriptor = account_config(account).descriptor
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    part = descriptor.with_suffix(".part")
    part.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(part, descriptor)


def _launch(urls: list[str], account: str | None = None) -> None:
    cfg = account_config(account)
    cfg.log.parent.mkdir(parents=True, exist_ok=True)
    log = open(cfg.log, "ab", buffering=0)
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
    args = chrome_args(urls, **_named(cfg))
    try:
        # Break away from the caller's job object so an agent's shell ending never takes Chrome with it.
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)


def start(show: bool = False, site: str = "gemini", account: str | None = None) -> str:
    """Start the bridge's Chrome if it is down; with `show`, bring a Gemini tab to the front."""
    cfg = account_config(account)
    kw = _named(cfg)
    if not CHROME.is_file():
        raise BridgeError(f"Google Chrome not found: {CHROME}")
    if health(**kw):
        if show:
            open_site(site, **kw)
            return f"已打开 {cfg.label} 窗口"
        return f"{cfg.label} 已在运行"
    if main_pid(**kw) is not None:
        return f"{cfg.label} 正在启动"
    _prepare_profile(**kw)
    _launch([SITES.get(site, SITES["gemini"])], **kw)
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        live = health(**kw)
        if live:
            descriptor = {"kind": "gemini-web-bridge", "pid": live["pid"], "cdp": _endpoint(cfg),
                          "profile": str(cfg.profile), "downloads": str(cfg.downloads),
                          "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            if kw:
                descriptor["account"] = cfg.name
            _write_descriptor(descriptor, **kw)
            return f"已打开 {cfg.label} 窗口" if show else f"已启动 {cfg.label}"
        time.sleep(0.5)
    raise BridgeError(f"{cfg.label} did not open its debugging port within {START_WAIT_SECONDS}s; see {cfg.log}")


def ensure_running(account: str | None = None) -> str:
    cfg = account_config(account)
    start(show=False, **_named(cfg))
    return _endpoint(cfg)


def open_site(site: str = "gemini", account: str | None = None) -> dict[str, Any]:
    """Open a site in a new foreground tab of the bridge window (CDP /json/new, then activate)."""
    endpoint = _endpoint(account_config(account))
    url = SITES.get(site, site if str(site).startswith("https://") else SITES["gemini"])
    target = _get_json("/json/new?" + urllib.parse.quote(url, safe=":/?=&%"), method="PUT", timeout=10,
                       endpoint=endpoint)
    # /json/activate answers with plain text, not JSON.
    urllib.request.urlopen(endpoint + f"/json/activate/{target['id']}", timeout=5).read()
    return {"id": target.get("id"), "url": url}


def stop(wait: float = 10.0, account: str | None = None) -> str:
    import psutil

    cfg = account_config(account)
    pid = main_pid(**_named(cfg))
    if pid is None:
        return f"{cfg.label} 未在运行"
    # Without /F, taskkill asks Chrome's windows to close, so the profile shuts down cleanly.
    subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and psutil.pid_exists(pid):
        time.sleep(0.3)
    forced = psutil.pid_exists(pid)
    if forced:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    try:
        cfg.descriptor.unlink()
    except OSError:
        pass
    return f"已停止 {cfg.label}（强制结束）" if forced else f"已停止 {cfg.label}"


def connect(playwright: Any, account: str | None = None) -> tuple[Any, Any]:
    """Attach Playwright to the bridge (starting it if needed); returns (browser, persistent context)."""
    cfg = account_config(account)
    browser = playwright.chromium.connect_over_cdp(ensure_running(**_named(cfg)))
    if not browser.contexts:
        raise BridgeError(f"{cfg.label} has no default browser context")
    return browser, browser.contexts[0]


def new_task_page(playwright: Any, url: str, timeout_ms: int = 60000,
                  account: str | None = None) -> tuple[Any, Any]:
    """A fresh tab in the signed-in profile, navigated to `url`. Close the page, never the browser."""
    browser, context = connect(playwright, **_named(account_config(account)))
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    return browser, page


def minimize_windows(cdp: Any) -> None:
    """Minimize every bridge window (Chrome ignores --start-minimized and STARTUPINFO for its first window)."""
    seen: set[int] = set()
    for target in cdp.send("Target.getTargets")["targetInfos"]:
        if target["type"] != "page":
            continue
        window = cdp.send("Browser.getWindowForTarget", {"targetId": target["targetId"]})["windowId"]
        if window not in seen:
            seen.add(window)
            cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "minimized"}})


def new_background_page(playwright: Any, url: str, timeout_ms: int = 60000,
                        account: str | None = None) -> tuple[Any, Any]:
    """Like new_task_page, but the tab opens without activating the window, and a bridge that was down
    is minimized right after it starts. For work driven purely over CDP (OAuth consent clicks)."""
    cfg = account_config(account)
    was_up = bool(health(**_named(cfg)))
    browser, context = connect(playwright, **_named(cfg))
    cdp = browser.new_browser_cdp_session()
    if not was_up:
        minimize_windows(cdp)
    # Playwright's new_page() activates the tab and restores a minimized window; a background target does not.
    with context.expect_page(timeout=timeout_ms) as created:
        cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
    page = created.value
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


def _pop_account(argv: list[str]) -> tuple[list[str], str | None]:
    """Strip `--account NAME` / `--account=NAME` from argv."""
    rest: list[str] = []
    account: str | None = None
    items = iter(argv)
    for item in items:
        if item == "--account":
            account = next(items, None)
            if account is None:
                raise BridgeError("--account needs a name")
        elif item.startswith("--account="):
            account = item.split("=", 1)[1]
        else:
            rest.append(item)
    return rest, account


def main(argv: list[str]) -> int:
    try:
        argv, account = _pop_account(list(argv))
        cfg = account_config(account)
        kw = _named(cfg)
        command = argv[0] if argv else "status"
        if command == "status":
            live = health(**kw)
            report = {"running": bool(live), **(live or {})}
            if kw and not live:
                report.update({"account": cfg.name, "cdp": cfg.endpoint, "profile": str(cfg.profile)})
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if live else 1
        if command == "start":
            site = argv[argv.index("--site") + 1] if "--site" in argv else "gemini"
            print(start(show="--show" in argv, site=site, **kw))
            return 0
        if command == "show":
            print(start(show=True, site=argv[1] if len(argv) > 1 else "gemini", **kw))
            return 0
        if command == "stop":
            print(stop(**kw))
            return 0
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
