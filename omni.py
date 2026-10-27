#!/usr/bin/env python3
"""Gemini Omni 1.1 Flash (gemini-omni-1.1-flash) video generation through the Gemini Web browser.

Runs Google AI Studio inside Route Manager's signed-in Gemini Web Chrome (gemini_web.py): each run
opens its own tab, sets aspect ratio / resolution / duration, attaches the media, sends a prompt
with Omni's role tags and saves the model's video. No API key, no other transport.

  python -X utf8 omni.py status
  python -X utf8 omni.py packet --video src.mp4 --image-ref style.png -p "..." [--out DIR]
  python -X utf8 omni.py run    --video src.mp4 --image-ref style.png -p "..." [--out DIR] [--prepare-only]

Role tags, in the order Omni expects them:
  [# Sources <VIDEO_0>@Video1]                       --video        edit this clip (<=10 s)
  [# Sources <PREVIOUS_VIDEO>@Video1]                --extend-video continue this clip (<=10 s)
  [# Sources <FIRST_FRAME>@Image1 <LAST_FRAME>@Image2] --first-frame / --last-frame
  [# Sources <FIRST_FRAME>@Image1 <LAST_FRAME>@Image1] --loop-frame
  [# References <IMAGE_REF_0>@ImageN ... <VIDEO_REF_0>@VideoM ...] --image-ref (<=6) / --video-ref (<=3, <=3 s)

No OS dialogs: files go in through the page's persistent `input.file-input` (set_input_files), and
the video is read inside the page (fetch of the gallery <video> blob), never via a download button.
An existing output.mp4 is never overwritten.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

import gemini_web

MODEL_ID = "gemini-omni-1.1-flash"
AISTUDIO_URL = f"https://aistudio.google.com/prompts/new_chat?model={MODEL_ID}"
RUNS = gemini_web.HOME / "runs"
RESOLUTIONS = ("360p", "720p", "1080p", "4k")
ASPECT_RATIOS = ("16:9", "9:16")
MIN_VIDEO_BYTES = 16384

# One-time account decisions AI Studio asks for. The run stops and leaves them to the account owner.
BLOCKING_DIALOGS = (
    ("Start creating with media", "consent_required",
     "AI Studio shows its one-time media upload acknowledgement. The account owner must click "
     "Acknowledge in the Gemini Web window; then re-run."),
    ("Temporary Chat is not supported", "drive_required",
     "AI Studio video generation needs conversations saved to Google Drive. The account owner must click "
     "Enable Google Drive in the Gemini Web window; then re-run."),
)
ERROR_PATTERNS = (
    "An internal error has occurred",
    "Failed to generate",
    "Something went wrong",
    "quota",
    "rate limit",
    "violat",
    "couldn't generate",
    "unable to generate",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def probe_video_duration(path: Path) -> float | None:
    try:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(str(path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        cap.release()
        if fps > 0 and frames > 0:
            return float(frames / fps)
    except Exception:
        pass
    return None


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or ("video/mp4" if path.suffix.lower() in (".mp4", ".m4v", ".mov", ".webm") else "application/octet-stream")


def build_packet(
    prompt: str,
    *,
    video: str | Path | None = None,
    extend_video: str | Path | None = None,
    first_frame: str | Path | None = None,
    last_frame: str | Path | None = None,
    loop_frame: str | Path | None = None,
    image_refs: list[str | Path] | None = None,
    video_refs: list[str | Path] | None = None,
    resolution: str = "720p",
    aspect_ratio: str = "16:9",
    continue_url: str | None = None,
    strict_duration: bool = True,
) -> dict[str, Any]:
    """Validate the media and compose the prompt with Omni's role tags (ImageN / VideoM are 1-based)."""
    image_refs = list(image_refs or [])
    video_refs = list(video_refs or [])
    if resolution not in RESOLUTIONS:
        raise ValueError(f"Unsupported resolution '{resolution}'. Allowed: {RESOLUTIONS}")
    if aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unsupported aspect_ratio '{aspect_ratio}'. Allowed: {ASPECT_RATIOS}")
    if last_frame and not first_frame:
        raise ValueError("--last-frame requires --first-frame.")
    if loop_frame and (first_frame or last_frame):
        raise ValueError("--loop-frame cannot be combined with --first-frame or --last-frame.")
    if video and extend_video:
        raise ValueError("Specify either --video (edit) or --extend-video (extend), not both.")
    if len(image_refs) > 6:
        raise ValueError("Omni 1.1 takes at most 6 reference images.")
    if len(video_refs) > 3:
        raise ValueError("Omni 1.1 takes at most 3 reference videos.")

    attachments: list[dict[str, Any]] = []
    counters = {"image": 0, "video": 0}
    sources: list[str] = []
    references: list[str] = []
    guidance: list[str] = []
    warnings: list[str] = []

    def add(kind: str, p: str | Path, role: str, tag: str, max_sec: float | None = None) -> str:
        path = Path(p).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{kind.capitalize()} file not found: {path}")
        entry: dict[str, Any] = {"kind": kind, "role": role, "tag": tag, "path": str(path), "name": path.name,
                                 "size_bytes": path.stat().st_size, "mime_type": guess_mime(path)}
        if kind == "video":
            dur = probe_video_duration(path)
            entry["duration_sec"] = round(dur, 3) if dur is not None else None
            if dur is not None and max_sec is not None and dur > max_sec + 0.15:
                msg = f"Video '{path.name}' for role '{role}' is {dur:.2f}s (Omni 1.1 limit is {max_sec:.0f}s)."
                if strict_duration:
                    raise ValueError(msg)
                warnings.append(msg)
        counters[kind] += 1
        entry["alias"] = f"{kind.capitalize()}{counters[kind]}"
        entry["index"] = len(attachments)
        attachments.append(entry)
        return entry["alias"]

    if loop_frame:
        alias = add("image", loop_frame, "loop_frame", "<FIRST_FRAME>/<LAST_FRAME>")
        sources += [f"<FIRST_FRAME>@{alias}", f"<LAST_FRAME>@{alias}"]
        guidance.append(f"Use {alias} as the first frame and the last frame.")
    elif first_frame:
        first = add("image", first_frame, "first_frame", "<FIRST_FRAME>")
        sources.append(f"<FIRST_FRAME>@{first}")
        if last_frame:
            last = add("image", last_frame, "last_frame", "<LAST_FRAME>")
            sources.append(f"<LAST_FRAME>@{last}")
            guidance.append(f"Use {first} as the starting frame and {last} as the ending frame.")
        else:
            guidance.append(f"Use {first} as the starting frame.")
    if video:
        alias = add("video", video, "edit_source", "<VIDEO_0>", max_sec=10.0)
        sources.append(f"<VIDEO_0>@{alias}")
        guidance.append(f"Use {alias} as the primary source video to edit. Keep everything else the same unless specified.")
    if extend_video:
        alias = add("video", extend_video, "extend_source", "<PREVIOUS_VIDEO>", max_sec=10.0)
        sources.append(f"<PREVIOUS_VIDEO>@{alias}")
        guidance.append(f"Extend {alias} seamlessly using its prior context.")
    for idx, p in enumerate(image_refs):
        references.append(f"<IMAGE_REF_{idx}>@{add('image', p, 'image_reference', f'<IMAGE_REF_{idx}>')}")
    if image_refs:
        guidance.append("Use the given reference image(s) as visual references for video generation. "
                        "The reference images should not be used as literal initial frames unless also declared in Sources.")
    for idx, p in enumerate(video_refs):
        references.append(f"<VIDEO_REF_{idx}>@{add('video', p, 'video_reference', f'<VIDEO_REF_{idx}>', max_sec=3.0)}")
    if video_refs:
        guidance.append("Use the given reference video(s) for motion/character reference. "
                        "Do not use reference videos as a direct source for in-place video editing.")

    if extend_video:
        task = "extend"
    elif video:
        task = "edit"
    elif image_refs or video_refs:
        task = "reference_to_video"
    elif first_frame or loop_frame:
        task = "image_to_video"
    else:
        task = "text_to_video"

    tags = " ".join(block for block in (
        f"[# Sources {' '.join(sources)}]" if sources else "",
        f"[# References {' '.join(references)}]" if references else "",
    ) if block)
    clean_prompt = prompt.strip()
    composed = " ".join(part for part in (tags, clean_prompt, " ".join(guidance)) if part).strip()
    return {
        "schema": "gemini-web-omni-packet/v1",
        "created_at": utc_now(),
        "model": MODEL_ID,
        "target_url": continue_url or AISTUDIO_URL,
        "task": task,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "raw_prompt": clean_prompt,
        "composed_prompt": composed,
        "prompt_sha256": hashlib.sha256(composed.encode("utf-8")).hexdigest(),
        "attachments": attachments,
        "warnings": warnings,
    }


def default_duration(packet: dict[str, Any]) -> int | None:
    """AI Studio's duration control takes 3-10 s; an edit keeps the length of its source video."""
    for att in packet.get("attachments", []):
        if att.get("kind") == "video" and att.get("duration_sec"):
            return max(3, min(10, int(-(-float(att["duration_sec"]) // 1))))
    return None


def blocking_dialog(page: Any) -> dict[str, Any] | None:
    for text, status, message in BLOCKING_DIALOGS:
        if page.get_by_text(text).count():
            return {"status": status, "message": message, "dialog": text}
    return None


def configure(page: Any, packet: dict[str, Any], duration: int | None) -> dict[str, Any]:
    """Set aspect ratio, resolution and duration in the run settings panel, then read them back."""
    panel = page.locator("ms-run-settings")
    if not (panel.count() and panel.first.is_visible()):
        page.locator('button[aria-label="Toggle run settings panel"]').click(timeout=10000)
        page.locator("ms-run-settings").first.wait_for(state="visible", timeout=10000)
    for label, value in (("Aspect ratio", packet.get("aspect_ratio")), ("Video Resolution", packet.get("resolution"))):
        if not value:
            continue
        page.locator(f'ms-run-settings mat-select[aria-label="{label}"]').click(timeout=10000)
        page.get_by_role("option", name=value, exact=True).click(timeout=10000)
        time.sleep(0.4)
    if duration:
        box = page.locator('ms-run-settings input[type=text][aria-label="Video duration in seconds"]')
        box.fill(str(duration))
        box.press("Tab")
        time.sleep(0.4)
    applied = page.evaluate(
        """() => {
            const panel = document.querySelector('ms-run-settings');
            const out = {};
            panel.querySelectorAll('mat-select').forEach(s => { out[s.getAttribute('aria-label')] = s.innerText.trim(); });
            const d = panel.querySelector('input[type=text][aria-label="Video duration in seconds"]');
            if (d) out['Video duration'] = d.value;
            return out;
        }"""
    )
    close = page.locator('ms-run-settings button:has-text("close")')
    if close.count():
        close.first.click()
    return applied


def video_candidates(page: Any) -> list[dict[str, Any]]:
    """Videos inside chat turns. AI Studio splits the user's message into one turn per attachment and its
    start page plays samples, so only a video inside `ms-video-generation-gallery-video` is model output."""
    return page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('ms-chat-turn').forEach((turn, index) => {
                turn.querySelectorAll('video').forEach(v => {
                    const src = v.currentSrc || v.src || (v.querySelector('source') || {}).src || '';
                    out.push({turn: index, src, ready: v.readyState, duration: v.duration || null,
                              width: v.videoWidth, height: v.videoHeight,
                              generated: Boolean(v.closest('ms-video-generation-gallery-video'))});
                });
            });
            return out;
        }"""
    )


def fetch_in_page(page: Any, src: str) -> bytes | None:
    b64 = page.evaluate(
        """async (src) => {
            try {
                const bytes = new Uint8Array(await (await fetch(src)).arrayBuffer());
                let binary = '';
                for (let i = 0; i < bytes.length; i += 0x8000) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
                }
                return btoa(binary);
            } catch (e) { return null; }
        }""",
        src,
    )
    return base64.b64decode(b64) if b64 else None


def turns_text(page: Any) -> list[str]:
    return page.evaluate("() => Array.from(document.querySelectorAll('ms-chat-turn')).map(t => t.innerText)")


def attachment_statuses(page: Any) -> list[str]:
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('ms-prompt-box .status-and-tokens')).map(e => (e.innerText || '').trim())"
    )


def run_page(page: Any, packet: dict[str, Any], out_dir: Path, *, wait_seconds: int, prepare_only: bool,
             duration: int | None) -> dict[str, Any]:
    """Settings, attachments, prompt, Run, then the model's video from the generation gallery."""
    log: list[str] = []
    applied = configure(page, packet, duration)
    log.append(f"settings applied: {applied}")

    paths = [att["path"] for att in packet["attachments"]]
    if paths:
        page.locator("input.file-input").set_input_files(paths)
        time.sleep(2.5)
        blocked = blocking_dialog(page)
        if blocked:
            return {**blocked, "settings": applied, "observed_url": page.url}
        quiet, deadline = 0, time.monotonic() + 240
        statuses: list[str] = []
        while quiet < 3 and time.monotonic() < deadline:
            busy = page.locator("[role=progressbar], mat-progress-spinner, mat-progress-bar").count()
            statuses = attachment_statuses(page)
            loading = any("loading" in s.lower() or not s for s in statuses) or (0 < len(statuses) < len(paths))
            quiet = quiet + 1 if (busy == 0 and not loading) else 0
            time.sleep(1.5)
        failed = [s for s in statuses if "fail" in s.lower() or "error" in s.lower()]
        if failed:
            return {
                "status": "attachment_failed",
                "message": f"AI Studio attachment token count failed: {failed}",
                "settings": applied,
                "observed_url": page.url,
                "log": log,
            }
        log.append(f"attached {len(paths)} files ({statuses})")

    box = page.locator("ms-prompt-box textarea")
    box = box.first if box.count() else page.locator("textarea").first
    box.fill(packet["composed_prompt"])
    shot = out_dir / "prepared_composer.png"
    page.screenshot(path=str(shot))
    if prepare_only:
        return {"status": "prepared_in_browser", "settings": applied, "attached_files": len(paths),
                "screenshot": str(shot), "observed_url": page.url, "log": log, "updated_at": utc_now()}

    # The settings toggle also has "Run" in its aria-label; the send button's text starts with "Run".
    run = page.locator("button", has_text=re.compile(r"^\s*Run\b"))
    run.first.wait_for(state="visible", timeout=10000)
    deadline = time.monotonic() + 60
    while run.first.get_attribute("aria-disabled") == "true" and time.monotonic() < deadline:
        time.sleep(1)
    run.first.click()
    submitted_at = utc_now()
    log.append(f"run clicked at {submitted_at}")

    deadline = time.monotonic() + wait_seconds
    last_src, saved, error_text = None, None, None
    while time.monotonic() < deadline:
        time.sleep(4)
        blocked = blocking_dialog(page)
        if blocked:
            return {**blocked, "settings": applied, "submitted_at": submitted_at, "observed_url": page.url, "log": log}
        model = [c for c in video_candidates(page) if c["generated"] and c["src"]]
        if model:
            src = model[-1]["src"]
            # Fetch once the same src has been seen on two polls and the element has data.
            if src == last_src and model[-1].get("ready", 0) >= 1:
                data = fetch_in_page(page, src)
                if data and len(data) > MIN_VIDEO_BYTES:
                    saved = out_dir / "output.mp4"
                    part = out_dir / "output.mp4.part"
                    part.write_bytes(data)
                    os.replace(part, saved)
                    log.append(f"saved model video ({len(data)} bytes, src={src[:80]})")
                    break
            last_src = src
        tail = "\n---\n".join(turns_text(page))[-1500:]
        hit = next((pat for pat in ERROR_PATTERNS if pat.lower() in tail.lower()), None)
        if hit and not model:
            error_text = tail
            break

    final_shot = out_dir / "final_page.png"
    try:
        page.screenshot(path=str(final_shot))
    except Exception:
        pass
    atomic_write_json(out_dir / "aistudio_dom.json", {
        "candidates": video_candidates(page),
        "turns_text": [t[:2000] for t in turns_text(page)],
    })
    return {
        "status": "completed" if saved else ("model_error" if error_text else "timeout_or_pending"),
        "settings": applied,
        "attached_files": len(paths),
        "submitted_at": submitted_at,
        "output_video": str(saved) if saved else None,
        "error_text": error_text,
        "observed_url": page.url,
        "screenshot": str(final_shot) if final_shot.exists() else None,
        "log": log,
        "completed_at": utc_now(),
    }


def run(packet: dict[str, Any], out_dir: Path, *, wait_seconds: int = 600, prepare_only: bool = False,
        duration: int | None = None) -> dict[str, Any]:
    """Run one packet in its own Gemini Web tab. The tab is closed on success and left in front otherwise,
    so the account owner can see what stopped it."""
    if (out_dir / "output.mp4").exists():
        raise FileExistsError(f"refusing to overwrite {out_dir / 'output.mp4'}; choose a new --out")
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"
    atomic_write_json(out_dir / "request.json", packet)
    (out_dir / "prompt_composed.txt").write_text(packet["composed_prompt"], encoding="utf-8")
    atomic_write_json(state_path, {"status": "launching_browser", "target_url": packet["target_url"],
                                   "started_at": utc_now(), "prompt_sha256": packet["prompt_sha256"]})

    with sync_playwright() as p:
        _, page = gemini_web.new_task_page(p, packet["target_url"])
        time.sleep(2.5)
        if gemini_web.needs_login(page):
            state = {"status": "login_required", "observed_url": page.url,
                     "message": "Sign in to Google once in the Gemini Web window (Route Manager tray → Gemini), then re-run."}
        elif "aistudio.google.com" not in page.url:
            state = {"status": "unexpected_page", "observed_url": page.url,
                     "message": "The target did not open Google AI Studio."}
        else:
            state = run_page(page, packet, out_dir, wait_seconds=wait_seconds, prepare_only=prepare_only,
                             duration=duration if duration is not None else default_duration(packet))
        state["conversation_url"] = page.url
        atomic_write_json(state_path, state)
        if state["status"] == "completed":
            try:
                page.close()
            except Exception:
                pass
        else:
            gemini_web.bring_to_front(page)
        return state


def status() -> dict[str, Any]:
    try:
        import playwright.sync_api  # noqa: F401

        playwright_error = None
    except Exception as exc:
        playwright_error = str(exc)
    live = gemini_web.health()
    return {
        "status": "ok" if playwright_error is None and gemini_web.CHROME.is_file() else "missing_runtime",
        "model": MODEL_ID,
        "target_url": AISTUDIO_URL,
        "playwright_error": playwright_error,
        "gemini_web": live or {"running": False, "cdp": gemini_web.CDP_ENDPOINT, "profile": str(gemini_web.PROFILE)},
        "runs": str(RUNS),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Playwright and Gemini Web readiness.")

    def media_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--prompt", "-p", default="", help="Instruction; may refer to <VIDEO_0>, <IMAGE_REF_0>, ...")
        p.add_argument("--prompt-file", help="Read the UTF-8 prompt from a file.")
        p.add_argument("--video", help="Source video to edit (<=10 s, <VIDEO_0>).")
        p.add_argument("--extend-video", help="Source video to extend (<=10 s, <PREVIOUS_VIDEO>).")
        p.add_argument("--first-frame", help="Starting keyframe (<FIRST_FRAME>).")
        p.add_argument("--last-frame", help="Ending keyframe (<LAST_FRAME>, needs --first-frame).")
        p.add_argument("--loop-frame", help="One image as both first and last frame.")
        p.add_argument("--image-ref", action="append", default=[], help="Reference image, up to 6 (<IMAGE_REF_N>).")
        p.add_argument("--video-ref", action="append", default=[], help="Reference video, up to 3, <=3 s (<VIDEO_REF_N>).")
        p.add_argument("--resolution", choices=RESOLUTIONS, default="720p")
        p.add_argument("--aspect-ratio", choices=ASPECT_RATIOS, default="16:9")
        p.add_argument("--continue-url", help="Existing AI Studio conversation to continue instead of a new chat.")
        p.add_argument("--out", "-o", help=f"Run directory (default {RUNS}/<timestamp>).")

    media_args(sub.add_parser("packet", help="Validate media and compose the prompt without sending."))
    p_run = sub.add_parser("run", help="Send to AI Studio in Gemini Web and save the model's video.")
    media_args(p_run)
    p_run.add_argument("--prepare-only", action="store_true", help="Stop before Run and leave the tab open.")
    p_run.add_argument("--wait", type=int, default=600, help="Max seconds to wait for the video after Run.")
    p_run.add_argument("--duration", type=int, help="Video duration 3-10 s; default the source video's length.")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.cmd == "status":
        info = status()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info["status"] == "ok" else 1

    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    packet = build_packet(
        prompt, video=args.video, extend_video=args.extend_video, first_frame=args.first_frame,
        last_frame=args.last_frame, loop_frame=args.loop_frame, image_refs=args.image_ref,
        video_refs=args.video_ref, resolution=args.resolution, aspect_ratio=args.aspect_ratio,
        continue_url=args.continue_url,
    )
    out_dir = Path(args.out).resolve() if args.out else RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.cmd == "packet":
        out_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(out_dir / "request.json", packet)
        (out_dir / "prompt_composed.txt").write_text(packet["composed_prompt"], encoding="utf-8")
        print(json.dumps({"status": "packet_ready", "out_dir": str(out_dir), "packet": packet}, ensure_ascii=False, indent=2))
        return 0
    result = run(packet, out_dir, wait_seconds=args.wait, prepare_only=args.prepare_only, duration=args.duration)
    print(json.dumps({"out_dir": str(out_dir), "result": result}, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in ("completed", "prepared_in_browser") else 2


if __name__ == "__main__":
    sys.exit(main())
