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
| [`colab_workstation.py`](colab_workstation.py) | On-demand Colab GPU workstation for 1–5 accounts on the official Colab CLI: per-account login, background consent in the account's bridge, HF/Drive staging profiles, idle auto-release. |
| [`test_gemini_web.py`](test_gemini_web.py), [`test_chrome_discovery.py`](test_chrome_discovery.py), [`test_omni.py`](test_omni.py), [`test_colab_workstation.py`](test_colab_workstation.py) | Unit test suites for browser lifecycle, multi-account isolation, CDP attachment, prompt/tag composition, consent automation boundaries, and staging/idle flows. |

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

## Colab GPU Workstation (`colab_workstation.py`)

On-demand Colab runtimes for 1–5 Google accounts, driven by the official [Colab CLI](https://github.com/googlecolab/google-colab-cli) (`google-colab-cli`, native on Windows). Nothing stays online: pick an account, bring a runtime up, run jobs, bring it down. A detached watcher releases a runtime left idle longer than `--idle-minutes` (default 20; `0` turns it off); a running job holds it off until the job ends or its timeout passes.

### Runtime lifetime and job timeouts

- Files on the VM disk (staged weights, outputs) live as long as the runtime; any number of `run`s can reuse them.
- Colab itself deletes a runtime once no code has run for a while (unpublished; idle timeouts only count while nothing executes; measured 2026-11-09: an idle CPU runtime on akiiwa's account was reclaimed after 179–182 min) and at its maximum lifetime (Pro+: up to 24 h of continuous execution). The watcher only releases earlier to save compute units; raise `--idle-minutes` or pass `0` to leave it to Colab. A `run` on a runtime Colab has reclaimed fails with `RuntimeLost` and drops the session record; `up` again.
- `run --timeout` is a wall-clock limit enforced here. colab-cli's own `--timeout` only bounds a stretch without output, and once it trips the CLI hangs instead of exiting; so at the limit the CLI is killed and the kernel restarted (`JobTimeout`; the VM disk is kept). A second `run` on a session whose job is still running is refused instead of queueing behind it.

```powershell
# Once: the CLI in its own venv (<DATA_DIR>/colab-cli; COLAB_CLI overrides)
python -X utf8 colab_workstation.py install-cli

# Once per account (max 5): register, open the account's bridge Chrome, sign in there
python -X utf8 colab_workstation.py onboard --account akiiwa --email akiiwacorps@gmail.com --auto-consent
python -X utf8 colab_workstation.py login --account akiiwa          # CLI OAuth, finished in the bridge

python -X utf8 colab_workstation.py status                           # bridges, CLI login, compute units, sessions
python -X utf8 colab_workstation.py up --account akiiwa --profile profiles\qwen-image.json --gpu L4 --session img
python -X utf8 colab_workstation.py run --account akiiwa --session img -f job.py [--timeout 3600]
python -X utf8 colab_workstation.py down --account akiiwa --session img    # last outputs copy, Drive flush, release
```

`--account auto` picks the logged-in account with the most compute units and no runtime of its own.

### Accounts and the browser

- Each account has its own colab-cli home `<HOME>/accounts/<acct>/colab-cli` (the CLI keeps `token.json` and `sessions.json` under `USERPROFILE`) and its own bridge Chrome; the CDP port comes from `<HOME>/accounts.json` (slots `17850, 17852, 17853, 17854, 17855`).
- The browser is needed for two Google consent pages, both opened in the account's bridge as a background tab (a bridge that was down is minimized right after it starts, so its window shows for ~1–2 s and never takes focus): the CLI login (once; the refresh token is reused) and Drive access for **every new runtime** (Colab asks again per VM; within one runtime remounting does not ask).
- The CLI hard-codes `authuser=0`, which is what breaks Drive mounting in a browser signed in to several accounts (CLI issue #103). The consent URL carries `login_hint`, and the bridge profile holds only its own account.
- With `--auto-consent` the bridge clicks Google's OAuth consent screens (including "Select all": a Drive grant without all scopes fails with `credential propagation was unsuccessful`). Without it, the bridge window comes to the front and the run waits for the owner. Password / 2-step sign-in, "Verify it's you" and CAPTCHAs are never automated.

### Profiles: what goes where

```json
{
  "hf":    [{"repo": "Comfy-Org/Qwen-Image-2.1", "files": ["vae/qwen_image_2.1_vae_bf16.safetensors"],
             "dest": "/content/models", "gated": false}],
  "drive": [{"src": "AI_Workstation/loras/style.safetensors", "dest": "/content/models/loras/style.safetensors", "mode": "copy"},
            {"src": "AI_Workstation/workflows.tar", "dest": "/content/workflows", "mode": "archive"}],
  "outputs": {"local": "/content/outputs", "drive": "AI_Workstation/outputs"}
}
```

Measured on a Colab runtime (Qwen-Image 2.1, 17.3 GB, 2026-11-09):

| Path | Throughput | 17.3 GB |
| :--- | :--- | :--- |
| Hugging Face → VM disk | 113–158 MB/s | ~2.5 min |
| VM disk → Drive (write) | 94–111 MB/s | ~3 min + 105 s flush |
| Drive → VM disk (fresh runtime) | 51–70 MB/s | ~5.4 min |

- **`hf`**: public weights are always downloaded from Hugging Face; keeping a Drive copy only makes startup slower. **Gated** repos (`"gated": true`) need a token: `HF_TOKEN` or `~/.cache/huggingface/token` on this machine is uploaded as a file to the VM's `~/.cache/huggingface/token` (mode 600) before staging. It never goes into code, `colab exec --env`, a notebook or Drive, because colab-cli logs executed code in plain text under `~/.config/colab-cli/history`; uploads log only paths. The VM disk, token included, is wiped when the runtime is released.
- **`drive`, `mode: copy`**: large private single files (LoRAs, private checkpoints, datasets). A tar gains nothing for already-compressed tensors.
- **`drive`, `mode: archive`**: trees of many small files (ComfyUI workflows, custom nodes, configs). Drive's FUSE mount pays a round trip per file, so these live on Drive as one `.tar`, extracted on the VM. `pack --src AI_Workstation/workflows --out AI_Workstation/workflows.tar` builds it on a running runtime.
- **`outputs`**: jobs write to the VM's local disk; after every `run` (failed ones included) new or changed files are copied to `<outputs.drive>/<session>/` on Drive, so a runtime that Colab reclaims on its own loses nothing (`--no-sync` defers this). `down` copies once more, runs `drive.flush_and_unmount()` and only then releases the runtime; if that copy fails, the runtime is kept (`sync_failed_runtime_kept`). Drive is the place results live; `fetch` is only for pulling a single file to this machine.

Runtime disk: ~242 GB free on a CPU runtime (0.08 CU/h); it does not persist across runtimes.

## Tests

```powershell
python -X utf8 -m unittest discover -s . -p "test_*.py" -v
```
