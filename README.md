# gemini-web

A persistent Google Chrome CDP bridge (`127.0.0.1:17849`) for automating **Google Gemini**, **Google AI Studio**, and **Google Labs Flow** using a real, persistent Google account sign-in, paired with a deterministic **Gemini Omni 1.1 Flash** (`gemini-omni-1.1-flash`) video generation and video-to-video runner in AI Studio.

Used both as a standalone browser automation toolkit and as the local Gemini Web bridge inside **AgentCompass** (via the `gemini-web` skill at `skills/gemini-web/SKILL.md`).

---

## Why Dedicated Chrome Over CDP (Not Electron)

- **Google Sign-In Compatibility**: Google blocks interactive account sign-in inside Electron embeds with *"This browser or app may not be secure"*, even when spoofing a standard Chrome User-Agent.
- **Isolated Non-Default Profile**: Standard Google Chrome only permits `--remote-debugging-port` on a non-default `--user-data-dir`. `gemini-web` launches your installed Google Chrome against a dedicated profile directory (`%LOCALAPPDATA%\Gemini Web Bridge\chrome-profile`) bound strictly to `127.0.0.1:17849`.
- **Sign In Once**: Sign in to your Google account once inside the Gemini Web window; cookies and session state persist across restarts without touching your everyday Chrome profile.
- **Unthrottled Background Rendering**: Launches with `--disable-features=CalculateNativeWinOcclusion`, `--disable-backgrounding-occluded-windows`, and `--disable-renderer-backgrounding` so background tabs continue rendering video playback, canvas frames, and timers when covered by other windows.
- **Prompt-Free Downloads**: Downloads go straight to `~/.gemini-web-bridge/downloads` without triggering OS `Save As` dialogs.

---

## Repository Contents

| File | Description |
| :--- | :--- |
| [`gemini_web.py`](gemini_web.py) | Browser lifecycle manager (`status`, `start`, `show`, `stop`) and Playwright loopback CDP helper (`connect`, `new_task_page`, `needs_login`, `bring_to_front`). |
| [`omni.py`](omni.py) | Gemini Omni 1.1 Flash (`gemini-omni-1.1-flash`) runner for Google AI Studio: composes role-tagged prompts, sets aspect ratio / resolution / duration, attaches source and reference media without OS file dialogs, runs generation, and extracts the output `.mp4`. |
| [`colab_workstation.py`](colab_workstation.py) | Dual-Ultra Colab + Google Drive workstation controller: per-account onboarding, bootstrap notebook generator, consent-safe Colab launch and A/B handoff. |
| [`test_gemini_web.py`](test_gemini_web.py), [`test_chrome_discovery.py`](test_chrome_discovery.py), [`test_omni.py`](test_omni.py), [`test_colab_workstation.py`](test_colab_workstation.py) | Unit test suites for browser lifecycle, multi-account isolation, CDP attachment, prompt/tag composition, bootstrap generation, and runner state transitions. |

---

## Browser Bridge (`gemini_web.py`)

### CLI Usage

```powershell
python -X utf8 gemini_web.py status                              # JSON health report, or "not running"
python -X utf8 gemini_web.py start [--show] [--site aistudio]    # Start the Chrome bridge (gemini | aistudio | flow | colab | drive)
python -X utf8 gemini_web.py show [gemini|aistudio|flow|colab|drive]  # Open a site tab and bring the window to the front
python -X utf8 gemini_web.py stop                                # Gracefully stop the dedicated Chrome instance
```

Every command accepts `--account <name>` (also `--account=<name>`). See [Multiple Google Accounts](#multiple-google-accounts).

### Python Playwright API

Each automation task opens its own tab and closes **only its tab** when finished—never close the shared browser instance:

```python
from playwright.sync_api import sync_playwright
import gemini_web

with sync_playwright() as p:
    browser, page = gemini_web.new_task_page(p, "https://gemini.google.com/app")
    try:
        # Perform task automation inside `page`
        ...
    finally:
        page.close()  # Always close the tab; never call browser.close()
```

### Environment Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `GEMINI_WEB_CHROME` | `C:\Program Files\Google\Chrome\Application\chrome.exe` | Path to the Google Chrome executable |
| `GEMINI_WEB_DATA_DIR` | `%LOCALAPPDATA%\Gemini Web Bridge` | Parent directory for `chrome-profile/` |
| `GEMINI_WEB_HOME` | `~/.gemini-web-bridge` | Runtime descriptor (`runtime/browser.json`), `downloads/`, and `runs/` |
| `GEMINI_WEB_CDP_PORT` | `17849` | Loopback Chrome DevTools Protocol port (`127.0.0.1:<port>`) |
| `GEMINI_WEB_CDP_PORT_<ACCOUNT>` | see below | CDP port of a named account (name upper-cased, `-` → `_`, e.g. `GEMINI_WEB_CDP_PORT_ULTRA_A`) |

### Multiple Google Accounts

Without `--account` (or with `--account default`) the bridge is exactly the single profile above, so `omni.py`, the AgentCompass tray and existing scripts are unaffected. A named account (`[A-Za-z0-9_-]{1,64}`) gets its own Chrome, so two accounts never share cookies, `authuser=0` sessions, `--user-data-dir` or `--remote-debugging-port`:

| | Default | Named account `<acct>` |
| :--- | :--- | :--- |
| Chrome profile | `<DATA_DIR>/chrome-profile` | `<DATA_DIR>/accounts/<acct>/chrome-profile` |
| Runtime descriptor | `<HOME>/runtime/browser.json` | `<HOME>/accounts/<acct>/runtime/browser.json` |
| Downloads | `<HOME>/downloads` | `<HOME>/accounts/<acct>/downloads` |
| CDP port | `17849` | `ultra_a` → `17850`, `ultra_b` → `17852`, others → stable hash in `17860–17959` |
| Window title | `Gemini Web` | `Gemini Web (<acct>)` |

`17851` is skipped on purpose: AgentCompass's `ports.py` reserves it for its account-login Chrome. Port precedence for a named account: `GEMINI_WEB_CDP_PORT_<ACCOUNT>`, then `<HOME>/accounts.json` (`{"accounts": {"lab-1": {"port": 18001}}}`), then the named slots, then the hash.

```powershell
python -X utf8 gemini_web.py show drive --account ultra_a
python -X utf8 gemini_web.py status --account ultra_b
```

```python
browser, page = gemini_web.new_task_page(p, "https://colab.research.google.com/", account="ultra_a")
```

`health()`, `start()`, `open_site()`, `stop()`, `ensure_running()`, `connect()` and `new_task_page()` all take `account=`.

---

## Gemini Omni 1.1 Video Runner (`omni.py`)

`omni.py` drives **Gemini Omni 1.1 Flash** (`gemini-omni-1.1-flash`) inside Google AI Studio (`https://aistudio.google.com/prompts/new_chat?model=gemini-omni-1.1-flash`). Because `gemini.google.com/videos` only accepts image attachments, all video-to-video editing, video extension, and multi-reference video generation run through AI Studio.

### CLI Commands

```powershell
# 1. Check browser & runner readiness
python -X utf8 omni.py status

# 2. Compose and validate the role-tagged prompt packet without touching the browser
python -X utf8 omni.py packet --video src.mp4 --image-ref style.png -p "Transform into cel-shaded animation"

# 3. Run full video generation in Google AI Studio (or add --prepare-only to stop before clicking Run)
python -X utf8 omni.py run --video src.mp4 --image-ref style.png --prompt-file prompt.txt `
    --resolution 720p --aspect-ratio 16:9 --out <new-run-dir> [--duration 4] [--prepare-only]
```

### Media Inputs & Omni Role Tags

`omni.py` automatically prepends the exact `[# Sources ...]` and `[# References ...]` role tags expected by Gemini Omni 1.1:

| Option | Media Input | Generated Role Tag |
| :--- | :--- | :--- |
| `--video <path>` | Source video clip to edit (`≤ 10 s`) | `[# Sources <VIDEO_0>@Video1]` |
| `--extend-video <path>` | Source video clip to continue/extend (`≤ 10 s`) | `[# Sources <PREVIOUS_VIDEO>@Video1]` |
| `--first-frame <path>` / `--last-frame <path>` | Start and/or end keyframe images | `[# Sources <FIRST_FRAME>@Image1 <LAST_FRAME>@Image2]` |
| `--loop-frame <path>` | Single image used as both start and end frame | `[# Sources <FIRST_FRAME>@Image1 <LAST_FRAME>@Image1]` |
| `--image-ref <path>` *(repeatable, up to 6)* | Character or style reference images | `[# References <IMAGE_REF_0>@ImageN ...]` |
| `--video-ref <path>` *(repeatable, up to 3)* | Motion reference clips (`≤ 3 s` each) | `[# References <VIDEO_REF_0>@VideoM ...]` |

- **Resolution**: `360p`, `720p` (default), `1080p`, or `4k`.
- **Aspect Ratio**: `16:9` (default) or `9:16`.
- **Duration**: Defaults to the source clip's probed duration rounded up and clamped to AI Studio's `3–10 s` range (overridable via `--duration`).

### Run Directory Artifacts & Status Codes

Each run writes its artifacts to `--out` (default `~/.gemini-web-bridge/runs/<timestamp>`):

- `request.json` and `prompt_composed.txt` — exact input parameters and composed role-tagged prompt.
- `state.json` — execution receipt including `conversation_url` and `status`:
  - `completed` — video generated and saved to `output.mp4`; the task tab is closed automatically.
  - `prepared_in_browser` — composer populated (`--prepare-only`); tab left open for manual inspection.
  - `login_required` — Google session is signed out; sign in once in the Gemini Web window and re-run.
  - `consent_required` — AI Studio displayed its one-time *"Start creating with media"* prompt; the account owner must click **Acknowledge** in the window.
  - `drive_required` — AI Studio displayed *"Temporary Chat is not supported"*; the account owner must click **Enable Google Drive** in the window.
  - `model_error` / `timeout_or_pending` — generation failed or timed out; the tab is brought to the front and diagnostic snapshots are saved.
- `prepared_composer.png`, `final_page.png`, and `aistudio_dom.json` — visual and DOM evidence.
- `output.mp4` — the generated video (minimum size verified; an existing `output.mp4` is never overwritten).

### Safety & Automation Guarantees

- **Zero OS Dialogs**: Media files are attached directly through the page's persistent `input.file-input` (`set_input_files`). Generated video bytes are fetched in-page from the generation gallery `<video>` (`ms-video-generation-gallery-video`), ignoring starter template videos and user input attachments, without clicking browser download buttons.
- **Account-Owner Consent Boundary**: One-time account prompts (`Acknowledge` media terms or `Enable Google Drive`) are never clicked automatically; the runner stops cleanly with `consent_required` or `drive_required` so the account owner remains in control.

---

## Dual-Ultra Colab + Google Drive Workstation (`colab_workstation.py`)

Two Google AI Ultra accounts (`ultra_a`, `ultra_b`) each run Colab with their own Compute Units and background execution, and share one Drive folder, `/MyDrive/AI_Workstation`, through a Drive shortcut. Each account is driven through its own bridge Chrome (`--account`).

```powershell
# Bridges and Colab tabs of the configured accounts (default ultra_a,ultra_b; override with COLAB_WORKSTATION_ACCOUNTS=a,b)
python -X utf8 colab_workstation.py status [--account ultra_a]

# Once per account: opens Drive + Colab in that account's Chrome; sign in and add the AI_Workstation shortcut
python -X utf8 colab_workstation.py onboard --account ultra_a

# Generate bootstrap_workstation.ipynb, workstation_bootstrap.py and workstation_bootstrap.sh; upload them to /MyDrive/AI_Workstation/
python -X utf8 colab_workstation.py init-bootstrap --out .\bootstrap `
    --server-cmd "vllm serve Qwen/Qwen3-8B --port {port}" --server-port 8000 --tunnel cloudflared

# Open the notebook in ultra_a's Chrome, check the runtime, Run all (or --prepare-only to only inspect)
python -X utf8 colab_workstation.py launch --account ultra_a --notebook-url <colab-url> [--out <run-dir>] [--prepare-only]

# Rolling handoff: bring ultra_b up first, verify it, write a receipt; ultra_a keeps serving until you retire it
python -X utf8 colab_workstation.py handoff --from-account ultra_a --to-account ultra_b --notebook-url <colab-url> `
    [--registry-dir "G:\My Drive\AI_Workstation\registry"]
```

### What the bootstrap notebook does

1. `drive.mount('/content/drive')` and verifies `/content/drive/MyDrive/AI_Workstation` (creates `registry/`, `env/`, `models/`, `logs/`).
2. Copies `env/*.tar.gz|*.tar|*.zip` archives and `models/*` from Drive to the VM's local disk `/content/local_cache` (one sequential read instead of thousands of small FUSE reads), sets `HF_HOME` / `HF_HUB_CACHE` under it and links `/content/models`.
3. Starts the configured server command (`{port}` is substituted), waits for the port, exposes it through `cloudflared` (quick tunnel URL parsed from its output), a custom command (EasyTier / Tailscale; give `TUNNEL_CMD` and `TUNNEL_URL`) or `none`.
4. Writes and heartbeats `/content/drive/MyDrive/AI_Workstation/registry/<account>.json` with `account`, `started_at`, `updated_at`, `gpu_name`, `server_port`, `tunnel_url` and `status` (`starting` → `running`, or `server_exited`).

The account is the `ACCOUNT` form field, or `auto`: `WORKSTATION_ACCOUNT`, else the first line of `/MyDrive/.ai_workstation_account` in that account's own My Drive (not the shared folder), else `unknown`.

### Launch states (`<out>/state.json`)

- `launched` — Run all (Ctrl+F9) was sent and no blocker appeared within `--observe-seconds`; `runtime` reports `connected`, `gpu` and visible `compute_units` when Colab shows them.
- `prepared_in_browser` — `--prepare-only`: notebook opened and runtime inspected, nothing executed.
- `login_required` — signed out, Colab's sign-in link, or a Google security challenge (`reason`: `signed_out`, `colab_signed_out`, `security_challenge`).
- `consent_required` — Drive permission dialog, OAuth consent popup, "not authored by Google" warning or a CAPTCHA (`reason`: `drive_mount_consent`, `oauth_consent`, `untrusted_notebook`, `captcha`).
- `unexpected_page` — the URL did not land on Colab.

Blocked states bring the relevant tab or popup to the front and save `blocked.png`; nothing is clicked. The account owner finishes the step and re-runs. `handoff` writes `handoff.json` with `ready`, the target's state and, with `--registry-dir`, the target's registry record (must be `running` and updated after the handoff began). It never stops the source runtime.

## Tests

```powershell
python -X utf8 -m unittest discover -s . -p "test_*.py" -v
```
