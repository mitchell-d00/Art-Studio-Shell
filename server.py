#!/usr/bin/env python3
"""Local Art Studio — tiny static + images-API proxy."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)

HOST = os.environ.get("ART_STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("ART_STUDIO_PORT", "8765"))
DEFAULT_API = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
SESSION_KEY = os.environ.get("OPENAI_API_KEY", "")
KEY_LOCK = threading.Lock()
MAX_BODY = 1_000_000  # 1 MB is plenty for a prompt


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


LOOPBACK_ONLY = _is_loopback(HOST)
# Host headers we accept when bound to loopback. Blocks DNS-rebinding tricks
# where an attacker's domain resolves to 127.0.0.1.
ALLOWED_HOSTS = {
    f"127.0.0.1:{PORT}",
    f"localhost:{PORT}",
    f"[::1]:{PORT}",
    f"{HOST}:{PORT}",
}

# Per-model request rules. Anything not listed falls back to "gpt-image".
MODEL_RULES = {
    "gpt-image": {
        "sizes": {"1024x1024", "1536x1024", "1024x1536", "auto"},
        "default_size": "1024x1024",
        "qualities": {"low", "medium", "high", "auto"},
        "max_n": 4,
        "output_format": True,
        "background": True,
    },
    "dall-e-3": {
        "sizes": {"1024x1024", "1792x1024", "1024x1792"},
        "default_size": "1024x1024",
        "qualities": {"standard", "hd"},
        "max_n": 1,
        "output_format": False,
        "background": False,
    },
}


def rules_for(model: str) -> dict:
    return MODEL_RULES["dall-e-3"] if model.startswith("dall-e-3") else MODEL_RULES["gpt-image"]


def set_key(value: str) -> None:
    global SESSION_KEY
    with KEY_LOCK:
        SESSION_KEY = value.strip()


def get_key() -> str:
    with KEY_LOCK:
        return SESSION_KEY


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "ArtStudio/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}")

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"))

    # --- request guards -------------------------------------------------

    def _host_ok(self) -> bool:
        if not LOOPBACK_ONLY:
            return True  # LAN mode: user opted out, warned at startup
        return (self.headers.get("Host") or "").lower() in ALLOWED_HOSTS

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # same-origin GETs, curl, etc.
        host = self.headers.get("Host") or ""
        return origin.lower() == f"http://{host}".lower()

    def _guard(self, is_post: bool) -> bool:
        if not self._host_ok():
            self._json(403, {"error": "Host not allowed."})
            return False
        if is_post:
            if not self._origin_ok():
                self._json(403, {"error": "Cross-origin request blocked."})
                return False
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                # Forces browsers into a CORS preflight, which this server never approves.
                self._json(415, {"error": "Content-Type must be application/json."})
                return False
        return True

    # --- routes ---------------------------------------------------------

    def do_GET(self) -> None:
        if not self._guard(is_post=False):
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = (ROOT / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(
                200,
                {
                    "has_key": bool(get_key()),
                    "base_url": DEFAULT_API,
                    "outputs": str(OUTPUTS),
                },
            )
            return
        if path.startswith("/outputs/"):
            name = Path(path).name
            target = OUTPUTS / name
            if not target.is_file() or target.parent != OUTPUTS:
                self._json(404, {"error": "not found"})
                return
            data = target.read_bytes()
            ctype = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, data, ctype)
            return
        self._json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        # Never grant CORS. Preflighted cross-origin POSTs stop here.
        self._json(405, {"error": "CORS not supported."})

    def do_POST(self) -> None:
        if not self._guard(is_post=True):
            return
        length = _to_int(self.headers.get("Content-Length"), -1)
        if length < 0 or length > MAX_BODY:
            self._json(413 if length > MAX_BODY else 411, {"error": "Bad or missing Content-Length."})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "Body must be a JSON object."})
            return

        path = self.path.split("?", 1)[0]
        if path == "/api/key":
            set_key(str(payload.get("key", "")))
            self._json(200, {"ok": True, "has_key": bool(get_key())})
            return
        if path == "/api/generate":
            self._generate(payload)
            return
        self._json(404, {"error": "not found"})

    # --- generation -----------------------------------------------------

    def _build_body(self, payload: dict) -> tuple[dict | None, str, str]:
        """Return (body, file_ext, error)."""
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            return None, "", "Prompt is empty."

        model = str(payload.get("model") or "gpt-image-1")
        rules = rules_for(model)

        size = str(payload.get("size") or rules["default_size"])
        if size not in rules["sizes"]:
            return None, "", f"{model} doesn't support size {size}. Use one of: {', '.join(sorted(rules['sizes']))}."

        n = max(1, min(_to_int(payload.get("n"), 1), rules["max_n"]))
        body: dict = {"model": model, "prompt": prompt, "n": n, "size": size}

        quality = str(payload.get("quality") or "").strip()
        if quality:
            if quality not in rules["qualities"]:
                return None, "", f"{model} doesn't support quality {quality}. Use one of: {', '.join(sorted(rules['qualities']))}."
            body["quality"] = quality

        ext = "png"
        if rules["output_format"]:
            fmt = str(payload.get("output_format") or "png")
            if fmt not in ("png", "jpeg", "webp"):
                fmt = "png"
            body["output_format"] = fmt
            ext = "jpg" if fmt == "jpeg" else fmt

            background = str(payload.get("background") or "").strip()
            if background and background != "auto" and rules["background"]:
                if background not in ("transparent", "opaque"):
                    return None, "", "Background must be auto, transparent, or opaque."
                if background == "transparent" and fmt == "jpeg":
                    return None, "", "Transparent backgrounds need PNG or WebP, not JPEG."
                body["background"] = background
        else:
            # dall-e-3: ask for base64 so we don't depend on short-lived URLs.
            body["response_format"] = "b64_json"

        return body, ext, ""

    def _generate(self, payload: dict) -> None:
        key = get_key()
        if not key:
            self._json(401, {"error": "No API key. Set OPENAI_API_KEY or paste one in the UI."})
            return

        body, ext, err = self._build_body(payload)
        if body is None:
            self._json(400, {"error": err})
            return

        req = urllib.request.Request(
            DEFAULT_API.rstrip("/") + "/images/generations",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                msg = parsed.get("error", {}).get("message") or detail
            except Exception:
                msg = detail or str(exc)
            self._json(exc.code, {"error": msg})
            return
        except Exception as exc:
            self._json(502, {"error": str(exc)})
            return

        images = []
        for item in data.get("data") or []:
            b64 = item.get("b64_json")
            url = item.get("url")
            raw_bytes = b""
            if b64:
                raw_bytes = base64.b64decode(b64)
            elif url:
                try:
                    with urllib.request.urlopen(url, timeout=120) as img_resp:
                        raw_bytes = img_resp.read()
                except Exception as exc:
                    images.append({"error": f"download failed: {exc}"})
                    continue
            if not raw_bytes:
                continue
            name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.{ext}"
            (OUTPUTS / name).write_bytes(raw_bytes)
            images.append(
                {
                    "src": f"/outputs/{name}",
                    "name": name,
                    "revised_prompt": item.get("revised_prompt"),
                }
            )
        self._json(
            200,
            {
                "images": images,
                "usage": data.get("usage"),
                "id": data.get("id"),
                "model": body["model"],
            },
        )


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    shown_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    url = f"http://{shown_host}:{PORT}/"
    print(f"Local Art Studio  →  {url}")
    print(f"Outputs           →  {OUTPUTS}")
    if not LOOPBACK_ONLY:
        print(
            f"\n  WARNING: bound to {HOST}, not loopback. Anyone who can reach this port\n"
            "  can generate images with your API key and replace the stored key.\n",
            file=sys.stderr,
        )
    print("Ctrl+C to quit.")
    if os.environ.get("ART_STUDIO_NO_BROWSER") != "1":
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
        httpd.server_close()


if __name__ == "__main__":
    main()
