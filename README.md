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
| [`test_gemini_web.py`](test_gemini_web.py), [`test_omni.py`](test_omni.py) | Unit test suites for browser lifecycle, CDP attachment, prompt/tag composition, and video runner state transitions. |

---

## Browser Bridge (`gemini_web.py`)

### CLI Usage

```powershell
python -X utf8 gemini_web.py status                              # JSON health report, or "not running"
python -X utf8 gemini_web.py start [--show] [--site aistudio]    # Start the Chrome bridge (gemini | aistudio | flow)
python -X utf8 gemini_web.py show [gemini|aistudio|flow]         # Open a site tab and bring the window to the front
python -X utf8 gemini_web.py stop                                # Gracefully stop the dedicated Chrome instance
```

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

## Tests

```powershell
python -X utf8 -m unittest test_gemini_web test_omni
```
