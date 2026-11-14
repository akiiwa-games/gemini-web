#!/usr/bin/env python3
"""Colab Cloud Orchestrator for Uncensored DeepSeek-V4.1-Flash-1017 (763B) + EasyTier + Uncensored Gateway.

100% Headless / Drive-Free Architecture (no browser OAuth popups on boot):
  - Local 3.27 MB payload (`serve/`, `strata_control/`, `tools/dsv41/`, `data/dsv41/`,
    `uncensored_gateway.py`, `cloud_worker.py`, `easytier-colab.toml`) is uploaded via `colab upload` (~8s).
  - Linux `easytier-core` v2.6.4 (`10.223.71.88/24`, `--no-tun --use-smoltcp`) and `uncensored_gateway.py`
    (`0.0.0.0:8081`) start IMMEDIATELY (~12s) so `http://10.223.71.88:8081/health` is reachable on EasyTier
    within ~25 seconds of boot and reports real-time download/pack/load telemetry.
  - Background `cloud_worker.py` on the Colab VM downloads `strata-dsv41` CUDA 13 engine (445 MB) +
    Uncensored 763B GGUF weights + 110.6 GB Engram + DSpark drafter + BF16 vision tower directly from
    Hugging Face with live byte/speed progress in `/content/cloud-state.json`, runs zero-copy `pack_gguf.py`,
    and starts `serve.server` + `strata-dsv41` on `127.0.0.1:18081`.

Usage:
  python -X utf8 dsv41_cloud.py up [--account akiiwa] [--gpu G4] [--variant coder|2.00bpw] [--no-wait]
  python -X utf8 dsv41_cloud.py status [--account akiiwa]
  python -X utf8 dsv41_cloud.py down [--account akiiwa]
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

import colab_workstation as cw

HERE = Path(__file__).resolve().parent
STRATA_ROOT = Path(r"E:\PROJECTS\local-llm\Strata")
BUNDLED_STRATA_RUNTIME = HERE / "assets" / "strata-runtime.tar.gz"
LOCAL_ET_TOML = Path(r"C:\ProgramData\AgentCompass\easytier\private\node.toml")
CLOUD_CONFIG_PATH = Path.home() / ".gemini-web-bridge" / "dsv41-cloud-config.json"

DEFAULT_ET_IP = "10.223.71.88"
DEFAULT_PORT = 8081
INTERNAL_STRATA_PORT = 18081
DEFAULT_SESSION = "dsv41"

DSV41_ENGINE_URL = (
    "https://github.com/Niko1221/Strata/releases/latest/download/"
    "strata-dsv41-0.1.59-linux-x86_64-cuda-cuda13.tar.gz"
)
EASYTIER_LINUX_URL = (
    "https://github.com/EasyTier/EasyTier/releases/download/v2.6.4/"
    "easytier-linux-x86_64-v2.6.4.zip"
)


def load_or_create_cloud_config(et_ip: str = DEFAULT_ET_IP) -> dict:
    if CLOUD_CONFIG_PATH.is_file():
        try:
            data = json.loads(CLOUD_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("api_key"):
                if et_ip and data.get("easytier_ip") != et_ip:
                    data["easytier_ip"] = et_ip
                    CLOUD_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
                return data
        except Exception:
            pass
    CLOUD_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "easytier_ip": et_ip,
        "port": DEFAULT_PORT,
        "api_key": "sk-strata-" + secrets.token_hex(16),
        "model_name": "deepseek-v4.1-flash-uncensored",
    }
    CLOUD_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def build_colab_easytier_toml(et_ip: str) -> str:
    """Derive a Colab --no-tun + --use-smoltcp EasyTier config from dsv41-cloud-config.json or local node.toml without printing secrets."""
    raw = ""
    if CLOUD_CONFIG_PATH.is_file():
        try:
            raw = json.loads(CLOUD_CONFIG_PATH.read_text(encoding="utf-8")).get("easytier_toml_template", "")
        except Exception:
            raw = ""
    if not raw and LOCAL_ET_TOML.is_file():
        try:
            raw = LOCAL_ET_TOML.read_text(encoding="utf-8")
        except Exception:
            raw = ""
    if not raw:
        raise RuntimeError(f"Cannot read EasyTier config from {CLOUD_CONFIG_PATH} or {LOCAL_ET_TOML}")
    out = raw
    out = re.sub(r'hostname\s*=\s*"[^"]*"', 'hostname = "colab-dsv41"', out)
    out = re.sub(r'instance_id\s*=\s*"[^"]*"', 'instance_id = "804d67b2-2ce0-4fd5-8de1-1733e8f4fe88"', out)
    out = re.sub(r'ipv4\s*=\s*"[^"]*"', f'ipv4 = "{et_ip}/24"', out)
    if "use_smoltcp" in out:
        out = re.sub(r'no_tun\s*=\s*(?:true|false)', 'no_tun = true', out)
        out = re.sub(r'use_smoltcp\s*=\s*(?:true|false)', 'use_smoltcp = true', out)
    else:
        out = re.sub(r'no_tun\s*=\s*(?:true|false)', 'no_tun = true\nuse_smoltcp = true', out)
    out = re.sub(r'relay_network_whitelist\s*=\s*"[^"]*"', 'relay_network_whitelist = "*"', out)
    return out


UNCENSORED_GATEWAY_PY = r'''#!/usr/bin/env python3
"""Uncensored Reverse-Proxy Gateway for Strata (`0.0.0.0:8081` -> `127.0.0.1:18081`).

1. Strips client-injected refusal / defensive-security system prompts and `<system-reminder>` blocks
   from `/v1/messages` and `/v1/chat/completions` before forwarding to Strata.
2. Enforces Bearer / x-api-key authentication for inference routes while leaving `/health`,
   `/cloud-status`, `/metrics`, and `/v1/models` accessible for health checks and discovery.
3. Serves rich real-time cloud staging/packing/loading telemetry on `/health` and `/cloud-status`
   from `/content/cloud-state.json` and `/content/strata-dsv41.log`.
"""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import urllib.request

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("STRATA_INTERNAL_PORT", "18081"))
LISTEN_HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("GATEWAY_PORT", "8081"))
EXPECTED_API_KEY = os.environ.get("STRATA_API_KEY", "")
MODEL_NAME = os.environ.get("STRATA_MODEL_NAME", "deepseek-v4.1-flash-uncensored")
STATE_FILE = Path("/content/cloud-state.json")
ENGINE_LOG = Path("/content/strata-dsv41.log")
WORKER_LOG = Path("/content/cloud-worker.log")

REFUSAL_PATTERNS = [
    re.compile(
        r"IMPORTANT:\s*Assist with (?:authorized security testing|defensive security).*?(?=\n\n|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"You must refuse to assist with, generate, or facilitate content related to.*?(?=\n\n|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"<system-reminder>\s*Whenever you read a file, you should consider whether it looks malicious.*?</system-reminder>",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"If the file seems malicious, you MUST refuse to work on it.*?(?=\n|\Z)",
        re.IGNORECASE,
    ),
]


def clean_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat in REFUSAL_PATTERNS:
        out = pat.sub("", out)
    return out


def sanitize_payload(body: bytes, path: str) -> bytes:
    if not body or not (path.startswith("/v1/messages") or path.startswith("/v1/chat/completions")):
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(data, dict):
        return body

    if "system" in data:
        sys_val = data["system"]
        if isinstance(sys_val, str):
            data["system"] = clean_text(sys_val)
        elif isinstance(sys_val, list):
            for block in sys_val:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = clean_text(block["text"])

    msgs = data.get("messages")
    if isinstance(msgs, list):
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = clean_text(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = clean_text(part["text"])

    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def tail_lines(path: Path, n: int = 12) -> list[str]:
    if not path.is_file():
        return []
    try:
        raw = path.read_bytes()[-16384:].decode("utf-8", errors="replace")
        return [ln.strip() for ln in raw.splitlines() if ln.strip()][-n:]
    except Exception:
        return []


def build_cloud_health() -> dict:
    cstate = {}
    if STATE_FILE.is_file():
        try:
            cstate = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    upstream_health = None
    upstream_metrics = None
    try:
        with urllib.request.urlopen(f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/health", timeout=1.2) as r:
            upstream_health = json.loads(r.read().decode("utf-8"))
    except Exception:
        pass

    if isinstance(upstream_health, dict) and (upstream_health.get("status") == "ok" or upstream_health.get("loaded") is True):
        try:
            with urllib.request.urlopen(f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/metrics", timeout=1.2) as r:
                raw_m = json.loads(r.read().decode("utf-8"))
                if isinstance(raw_m, dict):
                    upstream_metrics = {
                        "engine": raw_m.get("engine"),
                        "live": raw_m.get("live"),
                        "summary": raw_m.get("summary"),
                    }
        except Exception:
            pass

    logs = []
    for ln in (cstate.get("logs") or [])[-8:]:
        logs.append(ln)
    for ln in tail_lines(ENGINE_LOG, 10):
        logs.append(ln)
    if not logs:
        logs = tail_lines(WORKER_LOG, 10)

    loaded = bool(isinstance(upstream_health, dict) and (upstream_health.get("status") == "ok" or upstream_health.get("loaded") is True))
    status_str = "ok" if loaded else (cstate.get("phase") or "starting")
    return {
        "status": status_str,
        "loaded": loaded,
        "model_name": MODEL_NAME,
        "variant": cstate.get("variant", "coder"),
        "phase": "ready" if loaded else (cstate.get("phase") or "starting"),
        "phase_label": "在线就绪" if loaded else (cstate.get("phase_label") or "正在初始化云端节点..."),
        "downloaded_gb": cstate.get("downloaded_gb", 0.0),
        "total_gb": cstate.get("total_gb", 0.0),
        "speed_mbps": cstate.get("speed_mbps", 0.0),
        "current_file": cstate.get("current_file", ""),
        "file_index": cstate.get("file_index", 0),
        "file_count": cstate.get("file_count", 0),
        "gpu_name": cstate.get("gpu_name", "NVIDIA RTX PRO 6000 Blackwell"),
        "vram_used_mib": cstate.get("vram_used_mib"),
        "vram_total_mib": cstate.get("vram_total_mib"),
        "started_at": cstate.get("started_at"),
        "uptime_s": int(time.time() - float(cstate["started_at"])) if cstate.get("started_at") else 0,
        "upstream_health": upstream_health,
        "upstream_metrics": upstream_metrics,
        "log_tail": logs[-14:],
    }


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        clean_p = urlsplit(self.path).path
        if not EXPECTED_API_KEY or clean_p in ("/health", "/cloud-status", "/metrics", "/props", "/v1/models", "/models"):
            return True
        auth = self.headers.get("Authorization", "")
        x_api_key = self.headers.get("x-api-key", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
        if token == EXPECTED_API_KEY or x_api_key.strip() == EXPECTED_API_KEY:
            return True
        self._send_json(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
        return False

    def _proxy(self, method: str) -> None:
        if not self._check_auth():
            return
        clean_p = urlsplit(self.path).path
        if method == "GET" and clean_p in ("/health", "/cloud-status"):
            self._send_json(200, build_cloud_health())
            return
        if method == "GET" and clean_p in ("/v1/models", "/models"):
            # Even while loading, answer /v1/models so OMP/Harness model discovery succeeds immediately
            self._send_json(200, {
                "object": "list",
                "data": [{
                    "id": MODEL_NAME,
                    "object": "model",
                    "created": 1760000000,
                    "owned_by": "strata-cloud",
                }],
            })
            return

        content_len = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_len) if content_len > 0 else b""
        clean_body = sanitize_payload(raw_body, clean_p)

        fwd_headers = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in ("host", "content-length", "connection", "authorization", "x-api-key"):
                continue
            fwd_headers[k] = v
        fwd_headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        fwd_headers["Connection"] = "close"
        if clean_body:
            fwd_headers["Content-Length"] = str(len(clean_body))

        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=3600)
        try:
            conn.request(method, self.path, body=clean_body if clean_body else None, headers=fwd_headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for hk, hv in resp.getheaders():
                if hk.lower() in ("connection", "transfer-encoding"):
                    continue
                self.send_header(hk, hv)
            self.send_header("Connection", "close")
            self.end_headers()

            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                h = build_cloud_health()
                self._send_json(503, {
                    "status": h.get("phase"),
                    "error": {
                        "message": f"Cloud DeepSeek-V4.1 is still preparing ({h.get('phase_label')}): {exc}",
                        "type": "service_unavailable",
                    },
                })
            except Exception:
                pass
        finally:
            conn.close()

    def do_GET(self) -> None:
        self._proxy("GET")

    def do_POST(self) -> None:
        self._proxy("POST")

    def do_OPTIONS(self) -> None:
        self._proxy("OPTIONS")

    def do_DELETE(self) -> None:
        self._proxy("DELETE")


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), GatewayHandler)
    print(f"Uncensored Gateway listening on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}", flush=True)
    server.serve_forever()
'''


CLOUD_WORKER_PY = r'''#!/usr/bin/env python3
"""Background Worker on Colab VM: Downloads Engine + HF Weights with Live Progress, Packs, and Starts Strata."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

STATE_FILE = Path("/content/cloud-state.json")
CFG_FILE = Path("/content/strata-bundle/launch-config.json")


def query_gpu() -> tuple[str, int | None, int | None]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
            text=True,
            timeout=3,
        ).strip()
        if out:
            parts = [p.strip() for p in out.splitlines()[0].split(",")]
            if len(parts) >= 3:
                return parts[0], int(parts[1]), int(parts[2])
    except Exception:
        pass
    return "NVIDIA GPU", None, None


class StateReporter:
    def __init__(self, variant: str):
        self.started_at = time.time()
        self.variant = variant
        self.phase = "initializing"
        self.phase_label = "初始化云端环境..."
        self.downloaded_gb = 0.0
        self.total_gb = 0.0
        self.speed_mbps = 0.0
        self.current_file = ""
        self.file_index = 0
        self.file_count = 0
        self.logs: list[str] = []
        self.gpu_name, self.vram_used, self.vram_total = query_gpu()
        self._last_gpu_ts = time.time()

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.logs.append(line)
        self.logs = self.logs[-30:]
        self.flush()

    def flush(self) -> None:
        now = time.time()
        if now - self._last_gpu_ts > 5.0:
            self.gpu_name, self.vram_used, self.vram_total = query_gpu()
            self._last_gpu_ts = now
        data = {
            "started_at": self.started_at,
            "variant": self.variant,
            "phase": self.phase,
            "phase_label": self.phase_label,
            "downloaded_gb": round(self.downloaded_gb, 2),
            "total_gb": round(self.total_gb, 2),
            "speed_mbps": round(self.speed_mbps, 1),
            "current_file": self.current_file,
            "file_index": self.file_index,
            "file_count": self.file_count,
            "gpu_name": self.gpu_name,
            "vram_used_mib": self.vram_used,
            "vram_total_mib": self.vram_total,
            "logs": self.logs[-20:],
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)


def download_with_progress(url: str, dest: Path, st: StateReporter, completed_bytes: int, total_bytes: int) -> int:
    from concurrent.futures import ThreadPoolExecutor
    import threading

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1024 * 1024:
        sz = dest.stat().st_size
        st.log(f"跳过已存在文件 {dest.name} ({sz / 1e9:.2f} GB)")
        return sz

    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.unlink(missing_ok=True)

    # Probe file size and final CDN URL
    probe_req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Strata-Cloud/1.0)", "Range": "bytes=0-0"})
    with urllib.request.urlopen(probe_req, timeout=60) as presp:
        final_url = presp.geturl()
        cr = presp.headers.get("Content-Range") or ""
        if "/" in cr:
            file_len = int(cr.split("/")[-1])
        else:
            file_len = int(presp.headers.get("Content-Length") or 0)

    n_workers = 8 if file_len > 256 * 1024 * 1024 else 1
    fd = os.open(str(tmp), os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o644)
    t0 = time.time()
    lock = threading.Lock()
    state = {"got": 0, "last_flush": t0}

    def fetch_slice(byte_range: tuple[int, int]) -> None:
        start_b, end_b = byte_range
        pos = start_b
        for attempt in range(5):
            if pos > end_b:
                return
            hdrs = {"User-Agent": "Mozilla/5.0 (Strata-Cloud/1.0)", "Range": f"bytes={pos}-{end_b}"}
            req = urllib.request.Request(final_url, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    while pos <= end_b:
                        chunk = resp.read(min(8 * 1024 * 1024, end_b - pos + 1))
                        if not chunk:
                            break
                        os.pwrite(fd, chunk, pos)
                        pos += len(chunk)
                        with lock:
                            state["got"] += len(chunk)
                            now = time.time()
                            if now - state["last_flush"] >= 1.5:
                                state["last_flush"] = now
                                got_now = state["got"]
                                elapsed = max(now - t0, 0.01)
                                st.speed_mbps = (got_now / 1e6) / elapsed
                                st.downloaded_gb = (completed_bytes + got_now) / 1e9
                                pct = (got_now * 100.0 / file_len) if file_len else 0.0
                                st.phase_label = (
                                    f"拉取模型权重 [{st.file_index}/{st.file_count}] {dest.name[:24]}… "
                                    f"({st.downloaded_gb:.1f}/{st.total_gb:.1f} GB · {st.speed_mbps:.0f} MB/s · {pct:.0f}%)"
                                )
                                st.flush()
                if pos > end_b:
                    return
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(1.5)

    try:
        if file_len > 0 and n_workers > 1:
            step = (file_len + n_workers - 1) // n_workers
            slices = [(i * step, min(file_len - 1, (i + 1) * step - 1)) for i in range(n_workers) if i * step < file_len]
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                for fut in [pool.submit(fetch_slice, s) for s in slices]:
                    fut.result()
        else:
            fetch_slice((0, max(0, file_len - 1)))
    finally:
        os.close(fd)

    got = state["got"]
    tmp.replace(dest)
    elapsed = max(time.time() - t0, 0.01)
    mbps = (got / 1e6) / elapsed
    st.log(f"完成下载 {dest.name} ({got / 1e9:.2f} GB, {elapsed:.1f}s, {mbps:.0f} MB/s)")
    return got


def main() -> int:
    cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    variant = cfg["variant"]
    st = StateReporter(variant)
    st.log(f"启动云端部署工作流 (规格: {variant}, GPU: {st.gpu_name})")

    strata_root = Path("/content/strata-bundle/strata")

    try:
        # 1. Download Linux strata-dsv41 CUDA 13 engine
        eng_dest = strata_root / "engine-dsv41"
        exes = list(eng_dest.rglob("strata-dsv41")) if eng_dest.is_dir() else []
        if not exes:
            st.phase = "downloading_engine"
            st.phase_label = "正在拉取 Linux strata-dsv41 CUDA 13 推理引擎 (445 MB)..."
            st.flush()
            t0 = time.time()
            eng_tar = Path("/content/strata-dsv41-cuda.tar.gz")
            urllib.request.urlretrieve(cfg["dsv41_engine_url"], eng_tar)
            eng_dest.mkdir(parents=True, exist_ok=True)
            with tarfile.open(eng_tar, "r:gz") as t:
                t.extractall(eng_dest)
            eng_tar.unlink()
            exes = list(eng_dest.rglob("strata-dsv41"))
            st.log(f"推理引擎 strata-dsv41 下载解压完成 ({time.time() - t0:.1f}s)")

        exe = exes[0]
        exe.chmod(0o755)
        lib_dir = str(exe.parent)

        # 2. Download HF GGUF files directly with live progress (zero .cache overhead!)
        shm_dir = Path("/dev/shm/dsv41")
        shm_dir.mkdir(parents=True, exist_ok=True)

        if variant == "coder":
            model_dir = Path("/content/models/dsv41-coder")
            total_gb = 191.68
            downloads = [
                ("Abiray/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00001-of-00006.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00001-of-00006.gguf"),
                ("Abiray/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00002-of-00006.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00002-of-00006.gguf"),
                ("Abiray/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00003-of-00006.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00003-of-00006.gguf"),
                ("Abiray/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00004-of-00006.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00004-of-00006.gguf"),
                ("Abiray/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00005-of-00006.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00005-of-00006.gguf"),
                ("ISTA-DASLab/DeepSeek-V4.1-Flash-1017-GSQ-RCO-GGUF", "cb02422c900df489cea5833e8a92171224059b3c",
                 "DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf"),
                ("unsloth/DeepSeek-V4.1-Flash-1017-GGUF", "8a6ec9cf118e785a362ea94ee123c61c70553be6",
                 "mmproj-BF16.gguf",
                 shm_dir / "mmproj-BF16.gguf"),
                ("unsloth/DeepSeek-V4.1-Flash-1017-GGUF", "8a6ec9cf118e785a362ea94ee123c61c70553be6",
                 "dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf",
                 shm_dir / "dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf"),
            ]
            shards = [
                "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00001-of-00006.gguf",
                "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00002-of-00006.gguf",
                "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00003-of-00006.gguf",
                "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00004-of-00006.gguf",
                "DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-Coder-IQ2_XXS-00005-of-00006.gguf",
            ]
            profile_bin = strata_root / "data" / "dsv41" / "profile-coder.bin"
            engram_meta = model_dir / shards[0]
        else:
            model_dir = Path("/content/models/dsv41")
            total_gb = 257.80
            downloads = [
                ("AtomicChat/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00001-of-00004.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00001-of-00004.gguf"),
                ("AtomicChat/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00002-of-00004.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00002-of-00004.gguf"),
                ("AtomicChat/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00003-of-00004.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00003-of-00004.gguf"),
                ("AtomicChat/DeepSeek-V4.1-Flash-1017-Uncensored-GSQ-RCO-GGUF", "main",
                 "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00004-of-00004.gguf",
                 shm_dir / "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00004-of-00004.gguf"),
                ("ISTA-DASLab/DeepSeek-V4.1-Flash-1017-GSQ-RCO-GGUF", "cb02422c900df489cea5833e8a92171224059b3c",
                 "DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf",
                 model_dir / "DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf"),
                ("unsloth/DeepSeek-V4.1-Flash-1017-GGUF", "8a6ec9cf118e785a362ea94ee123c61c70553be6",
                 "UD-Q8_K_XL/DeepSeek-V4.1-Flash-1017-UD-Q8_K_XL-00001-of-00009.gguf",
                 shm_dir / "DeepSeek-V4.1-Flash-1017-UD-Q8_K_XL-00001-of-00009.gguf"),
                ("unsloth/DeepSeek-V4.1-Flash-1017-GGUF", "8a6ec9cf118e785a362ea94ee123c61c70553be6",
                 "mmproj-BF16.gguf",
                 shm_dir / "mmproj-BF16.gguf"),
                ("unsloth/DeepSeek-V4.1-Flash-1017-GGUF", "8a6ec9cf118e785a362ea94ee123c61c70553be6",
                 "dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf",
                 shm_dir / "dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf"),
            ]
            shards = [
                "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00001-of-00004.gguf",
                "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00002-of-00004.gguf",
                "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00003-of-00004.gguf",
                "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00004-of-00004.gguf",
            ]
            profile_bin = strata_root / "data" / "dsv41" / "profile-2.00.bin"
            engram_meta = shm_dir / "DeepSeek-V4.1-Flash-1017-UD-Q8_K_XL-00001-of-00009.gguf"

        model_dir.mkdir(parents=True, exist_ok=True)
        st.phase = "downloading_weights"
        st.total_gb = total_gb
        st.file_count = len(downloads)
        completed_bytes = 0
        for idx, (repo, rev, rel_path, dst_path) in enumerate(downloads, 1):
            st.file_index = idx
            st.current_file = dst_path.name
            url = f"https://huggingface.co/{repo}/resolve/{rev}/{rel_path}"
            got = download_with_progress(url, dst_path, st, completed_bytes, int(total_gb * 1e9))
            completed_bytes += got
            st.downloaded_gb = completed_bytes / 1e9
            st.flush()

        if variant != "coder":
            shm_shard4 = shm_dir / "DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ2_XXS-00004-of-00004.gguf"
            link_shard4 = model_dir / shm_shard4.name
            if shm_shard4.is_file() and not link_shard4.exists():
                os.symlink(str(shm_shard4), str(link_shard4))

        engram_file = model_dir / "DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf"
        dspark_file = shm_dir / "dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf"
        mmproj_file = shm_dir / "mmproj-BF16.gguf"
        pack_dir = Path("/content/packs/dsv41-uncensored")

        # 3. Zero-copy pack_gguf.py
        if not (pack_dir / "tensors.txt").is_file():
            st.phase = "packing"
            st.phase_label = "正在执行零拷贝 GGUF 索引打包 (pack_gguf.py)..."
            st.flush()
            t0 = time.time()
            cmd = [
                sys.executable,
                str(strata_root / "tools" / "dsv41" / "pack_gguf.py"),
                "--model", str(model_dir),
                "--shards", *shards,
                "--engram", str(engram_file),
                "--engram-meta", str(engram_meta),
                "--dspark-gguf", str(dspark_file),
                "--dspark-noise-token", "128799",
                "--dspark-markov-rank", "256",
                "--mmproj", str(mmproj_file),
                "--out", str(pack_dir),
            ]
            subprocess.run(cmd, check=True)
            if profile_bin.is_file():
                shutil.copy2(profile_bin, pack_dir / "expert-profile.bin")
            st.log(f"零拷贝打包完成 ({time.time() - t0:.1f}s)")

        # 4. Start serve.server + strata-dsv41
        st.phase = "loading_engine"
        st.phase_label = "正在将 763B 权重载入 RTX PRO 6000 显存与内存 (约 2–4 分钟)..."
        st.flush()

        internal_port = int(cfg["internal_port"])
        context_len = int(cfg.get("context", 262144))
        parallel_n = int(cfg.get("parallel", 2))

        run_cfg = {
            "exe": str(exe),
            "args": [
                "--pack", str(pack_dir),
                "--max-context", str(context_len),
                "--eos", "1",
                "--vision", "on",
                "--dspark", "auto",
                "--dspark-conf", "0.65",
                "--reserve-mib", "900",
                "--parallel", str(parallel_n),
            ],
            "cwd": str(strata_root),
            "tokenizer": str(pack_dir / "tokenizer"),
            "model_name": cfg["model_name"],
            "arch": "deepseek41",
            "log": "/content/strata-dsv41.log",
            "lib_dirs": [lib_dir],
            "host": "127.0.0.1",
            "port": internal_port,
            "engine_ready_s": 3600,
            "open_browser": False,
        }
        run_cfg_path = Path("/content/strata-dsv41.json")
        run_cfg_path.write_text(json.dumps(run_cfg, indent=2), encoding="utf-8")

        already_ready = False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{internal_port}/health", timeout=2) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("status") == "ok" or body.get("loaded") is True:
                    already_ready = True
        except Exception:
            pass

        if not already_ready:
            subprocess.run(["pkill", "-f", "serve.server"], check=False)
            subprocess.run(["pkill", "-f", "strata-dsv41"], check=False)

            env = os.environ.copy()
            env["PYTHONPATH"] = f"{strata_root}:{strata_root / 'tools'}"
            env["LD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('LD_LIBRARY_PATH', '')}"

            srv_log = open("/content/strata-server.log", "ab")
            subprocess.Popen(
                [sys.executable, "-m", "serve.server", "--engine", "strata", "--config", str(run_cfg_path),
                 "--host", "127.0.0.1", "--port", str(internal_port)],
                cwd=str(strata_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=srv_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        t0 = time.time()
        while time.time() - t0 < 1200:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{internal_port}/health", timeout=3) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    if body.get("status") == "ok" or body.get("loaded") is True:
                        st.phase = "ready"
                        st.phase_label = "在线运行"
                        st.log(f"Strata 763B 引擎就绪 ({time.time() - t0:.1f}s)")
                        return 0
            except Exception:
                pass
            st.flush()
            time.sleep(3)

        st.phase = "error"
        st.phase_label = "引擎加载超时，请检查日志"
        st.log("等待 Strata 引擎就绪超时")
        return 1
    except Exception as exc:
        st.phase = "error"
        st.phase_label = f"启动异常: {exc}"
        st.log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
'''


def create_local_payload_tar(et_ip: str, launch_cfg: dict) -> Path:
    """Create a local tar.gz containing Strata Python code + uncensored_gateway.py + cloud_worker.py + easytier-colab.toml."""
    tmp = tempfile.NamedTemporaryFile(prefix="strata-local-", suffix=".tar.gz", delete=False)
    tmp.close()
    et_toml = build_colab_easytier_toml(et_ip)

    with tarfile.open(tmp.name, "w:gz") as tar:
        if STRATA_ROOT.exists():
            for folder in ("serve", "strata_control", "tools", "data/dsv41"):
                src_dir = STRATA_ROOT / folder
                if not src_dir.exists():
                    raise RuntimeError(f"Missing Strata directory: {src_dir}")
                for path in src_dir.rglob("*"):
                    if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                        continue
                    if path.is_file():
                        rel = path.relative_to(STRATA_ROOT).as_posix()
                        tar.add(str(path), arcname=f"strata/{rel}")
        elif BUNDLED_STRATA_RUNTIME.is_file():
            with tarfile.open(BUNDLED_STRATA_RUNTIME, "r:gz") as btar:
                for member in btar.getmembers():
                    if member.isfile():
                        fobj = btar.extractfile(member)
                        if fobj is not None:
                            tar.addfile(member, fobj)
        else:
            raise RuntimeError(f"Neither {STRATA_ROOT} nor {BUNDLED_STRATA_RUNTIME} found")

        for arcname, content in (
            ("strata/uncensored_gateway.py", UNCENSORED_GATEWAY_PY),
            ("strata/cloud_worker.py", CLOUD_WORKER_PY),
            ("launch-config.json", json.dumps(launch_cfg, ensure_ascii=False, indent=2)),
        ):
            raw_b = content.encode("utf-8")
            ti = tarfile.TarInfo(arcname)
            ti.size = len(raw_b)
            ti.mode = 0o755 if arcname.endswith(".py") else 0o600
            tar.addfile(ti, io.BytesIO(raw_b))

        et_bytes = et_toml.encode("utf-8")
        ti_et = tarfile.TarInfo("easytier/easytier-colab.toml")
        ti_et.size = len(et_bytes)
        ti_et.mode = 0o600
        tar.addfile(ti_et, io.BytesIO(et_bytes))

    return Path(tmp.name)


REMOTE_BOOTSTRAP_SCRIPT = r'''
import json, os, shutil, subprocess, sys, tarfile, time, urllib.request, zipfile
from pathlib import Path

bundle = Path("/content/strata-bundle")
bundle.mkdir(parents=True, exist_ok=True)
strata_root = bundle / "strata"
et_dir = bundle / "easytier"

local_tar = Path("/content/_local_payload.tar.gz")
if local_tar.is_file():
    with tarfile.open(local_tar, "r:gz") as t:
        t.extractall(bundle)
    local_tar.unlink()

cfg = json.loads((bundle / "launch-config.json").read_text(encoding="utf-8"))

# 1. Download & start Linux EasyTier v2.6.4 immediately
et_core = et_dir / "easytier-core"
et_cli = et_dir / "easytier-cli"
et_toml = et_dir / "easytier-colab.toml"
if not et_core.is_file():
    t0 = time.time()
    et_zip = Path("/content/easytier-linux.zip")
    urllib.request.urlretrieve(cfg["easytier_url"], et_zip)
    with zipfile.ZipFile(et_zip, "r") as zf:
        zf.extractall("/content/et_tmp")
    et_zip.unlink()
    et_dir.mkdir(parents=True, exist_ok=True)
    for bin_name in ("easytier-core", "easytier-cli"):
        found = list(Path("/content/et_tmp").rglob(bin_name))
        shutil.copy2(found[0], et_dir / bin_name)
        (et_dir / bin_name).chmod(0o755)
    shutil.rmtree("/content/et_tmp", ignore_errors=True)

subprocess.run(["pkill", "-f", "easytier-core"], check=False)
et_log = open("/content/easytier.log", "ab")
subprocess.Popen(
    [str(et_core), "-c", str(et_toml)],
    stdin=subprocess.DEVNULL,
    stdout=et_log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)

# 2. Start Uncensored Gateway on 0.0.0.0:8081 immediately
subprocess.run(["pkill", "-f", "uncensored_gateway.py"], check=False)
env = os.environ.copy()
env["STRATA_INTERNAL_PORT"] = str(cfg["internal_port"])
env["GATEWAY_PORT"] = str(cfg["port"])
env["STRATA_API_KEY"] = cfg["api_key"]
env["STRATA_MODEL_NAME"] = cfg["model_name"]

gw_log = open("/content/uncensored-gateway.log", "ab")
subprocess.Popen(
    [sys.executable, str(strata_root / "uncensored_gateway.py")],
    cwd=str(strata_root),
    env=env,
    stdin=subprocess.DEVNULL,
    stdout=gw_log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)

# 3. Start background cloud_worker.py only if neither cloud_worker.py nor strata-dsv41 is already active
existing_wk = subprocess.run(["pgrep", "-f", "cloud_worker.py|strata-dsv41"], capture_output=True, text=True).stdout.strip()
if existing_wk:
    wk_pid = int(existing_wk.splitlines()[0])
else:
    wk_log = open("/content/cloud-worker.log", "ab")
    wk_proc = subprocess.Popen(
        [sys.executable, str(strata_root / "cloud_worker.py")],
        cwd=str(strata_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=wk_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    wk_pid = wk_proc.pid

time.sleep(3.5)
peer_out = subprocess.run([str(et_cli), "peer"], capture_output=True, text=True).stdout
print("BOOTSTRAP", json.dumps({
    "status": "bootstrapped",
    "worker_pid": wk_pid,
    "reused_worker": bool(existing_wk),
    "easytier_ip": cfg["easytier_ip"],
    "port": cfg["port"],
    "peers": peer_out.strip(),
}), flush=True)
'''


def allocate_gpu_session(account: str, gpu: str, session: str, idle_minutes: int) -> dict:
    """Allocate a fresh Colab GPU VM without blocking on stage_data."""
    if account == "auto":
        account = cw.pick_account(cw.status().get("accounts", []))
    entry = cw.account_entry(account)
    name = entry["name"]
    if not cw.token_path(name).is_file():
        login_res = cw.login(name)
        if login_res["status"] not in ("logged_in", "already_logged_in"):
            return {"account": name, "status": "login_" + str(login_res["status"]), "login": login_res}

    existing = cw.load_state(name).get("sessions", {}).get(session)
    if not existing:
        args = ["new", "-s", session] + (["--gpu", gpu] if gpu else [])
        t0 = time.monotonic()
        try:
            cw.check_cli(name, *args, timeout=600)
        except cw.WorkstationError:
            try:
                cw.run_cli(name, "stop", "-s", session, timeout=120)
            except subprocess.TimeoutExpired:
                pass
            raise
        cw.update_session(
            name,
            session,
            created_at=cw.utc_now(),
            last_used=cw.utc_now(),
            gpu=gpu or "cpu",
            profile="dsv41-cloud",
            idle_minutes=idle_minutes,
            outputs=None,
            mounted=False,
        )
        if idle_minutes > 0:
            wpid = cw.spawn_watcher(name, session)
            cw.update_session(name, session, watcher_pid=wpid)
        return {"status": "ready", "account": name, "session": session, "new_seconds": round(time.monotonic() - t0, 1)}
    else:
        cw.touch(name, session)
        return {"status": "ready", "account": name, "session": session, "reused": True}


def cmd_up(
    account: str,
    gpu: str,
    variant: str,
    session: str,
    idle_minutes: int,
    context: int,
    parallel: int,
    et_ip: str,
    wait_ready: bool = True,
) -> dict:
    cloud_cfg = load_or_create_cloud_config(et_ip=et_ip)
    alloc = allocate_gpu_session(account, gpu=gpu, session=session, idle_minutes=idle_minutes)
    if alloc.get("status") != "ready":
        return alloc
    acct = alloc["account"]

    launch_cfg = {
        "variant": variant,
        "easytier_ip": cloud_cfg["easytier_ip"],
        "port": cloud_cfg["port"],
        "internal_port": INTERNAL_STRATA_PORT,
        "api_key": cloud_cfg["api_key"],
        "model_name": cloud_cfg["model_name"],
        "context": context,
        "parallel": parallel,
        "dsv41_engine_url": DSV41_ENGINE_URL,
        "easytier_url": EASYTIER_LINUX_URL,
    }

    local_tar = create_local_payload_tar(cloud_cfg["easytier_ip"], launch_cfg)
    try:
        cw.check_cli(acct, "upload", "-s", session, str(local_tar), "content/_local_payload.tar.gz", timeout=300)
    finally:
        local_tar.unlink(missing_ok=True)

    out = cw.exec_code(acct, session, REMOTE_BOOTSTRAP_SCRIPT, timeout=180)
    boot_rows = cw.parse_tagged(out, "BOOTSTRAP")

    if wait_ready:
        ip = cloud_cfg["easytier_ip"]
        port = cloud_cfg["port"]
        last_keepalive = time.monotonic()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3600:
            # Keep Colab kernel active every 55s with a 2-second heartbeat so Google never reclaims the VM during download
            if time.monotonic() - last_keepalive >= 55.0:
                try:
                    cw.exec_code(acct, session, "print('keepalive')", timeout=30)
                except Exception:
                    pass
                last_keepalive = time.monotonic()

            try:
                with urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=3) as resp:
                    h = json.loads(resp.read().decode("utf-8"))
                    phase = h.get("phase")
                    print(
                        f"[dsv41-cloud] {h.get('phase_label')} "
                        f"({h.get('downloaded_gb', 0)}/{h.get('total_gb', 0)} GB, {h.get('speed_mbps', 0)} MB/s)",
                        flush=True,
                    )
                    if h.get("loaded") is True or phase == "ready":
                        break
                    if phase == "error":
                        return {"status": "error", "health": h}
            except Exception:
                pass
            time.sleep(5.0)

    return {
        "status": "ready" if wait_ready else "bootstrapped",
        "account": acct,
        "session": session,
        "gpu": gpu,
        "variant": variant,
        "bootstrap": boot_rows,
        "endpoint_openai": f"http://{cloud_cfg['easytier_ip']}:{cloud_cfg['port']}/v1",
        "endpoint_anthropic": f"http://{cloud_cfg['easytier_ip']}:{cloud_cfg['port']}",
        "model_name": cloud_cfg["model_name"],
        "api_key_file": str(CLOUD_CONFIG_PATH),
    }


def cmd_status(account: str | None, et_ip: str, include_colab: bool = True) -> dict:
    cloud_cfg = load_or_create_cloud_config(et_ip=et_ip)
    ip = cloud_cfg["easytier_ip"]
    port = cloud_cfg["port"]
    health_status = None
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=2.0) as resp:
            health_status = json.loads(resp.read().decode("utf-8"))
            health_status["reachable"] = True
    except Exception as exc:
        health_status = {"reachable": False, "error": str(exc)}
    ws_status = cw.status([account] if account else None, usage=True) if include_colab else None
    return {
        "endpoint_openai": f"http://{ip}:{port}/v1",
        "endpoint_anthropic": f"http://{ip}:{port}",
        "model_name": cloud_cfg["model_name"],
        "api_key_file": str(CLOUD_CONFIG_PATH),
        "easytier_gateway_health": health_status,
        "colab": ws_status,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_u = sub.add_parser("up", help="Start GPU runtime, pull HF weights, pack & serve on EasyTier")
    p_u.add_argument("--account", default="akiiwa")
    p_u.add_argument("--gpu", default="G4", choices=cw.GPUS)
    p_u.add_argument("--variant", default="coder", choices=("coder", "2.00bpw"))
    p_u.add_argument("--session", default=DEFAULT_SESSION)
    p_u.add_argument("--idle-minutes", type=int, default=120)
    p_u.add_argument("--context", type=int, default=262144)
    p_u.add_argument("--parallel", type=int, default=2)
    p_u.add_argument("--et-ip", default=DEFAULT_ET_IP)
    p_u.add_argument("--no-wait", action="store_true", help="Return right after EasyTier + Gateway bootstrap (~25s)")

    p_s = sub.add_parser("status", help="Check EasyTier endpoint health and Colab runtime status")
    p_s.add_argument("--account", default="akiiwa")
    p_s.add_argument("--et-ip", default=DEFAULT_ET_IP)
    p_s.add_argument("--fast", action="store_true", help="Only probe EasyTier /health without colab-cli usage call")

    p_d = sub.add_parser("down", help="Stop the GPU runtime and release billing")
    p_d.add_argument("--account", default="akiiwa")
    p_d.add_argument("--session", default=DEFAULT_SESSION)
    p_d.add_argument("--no-sync", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "up":
        res = cmd_up(
            args.account,
            args.gpu,
            args.variant,
            args.session,
            args.idle_minutes,
            args.context,
            args.parallel,
            args.et_ip,
            wait_ready=not args.no_wait,
        )
    elif args.cmd == "status":
        res = cmd_status(args.account, args.et_ip, include_colab=not args.fast)
    else:
        res = cw.down(args.account, args.session, sync=False)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
