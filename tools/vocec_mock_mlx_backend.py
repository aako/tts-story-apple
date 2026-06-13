#!/usr/bin/env python3
"""Tiny localhost-only mock backend for the Vocec MLX Local adapter."""
from __future__ import annotations

import argparse
import json
import logging
import math
import struct
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7860
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_DURATION_SECONDS = 1.0
ALLOWED_BIND_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_REQUEST_BYTES = 1024 * 1024

logger = logging.getLogger("vocec_mock_mlx_backend")


class JsonError(ValueError):
    """Request validation error with an HTTP status code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class VocecMockHandler(BaseHTTPRequestHandler):
    server_version = "VocecMockMLX/1.0"

    def do_GET(self) -> None:
        if self.path.rstrip("/") != "/health":
            self._send_json(404, {"ok": False, "success": False, "error": "Not found"})
            return
        self._send_json(
            200,
            {
                "ok": True,
                "success": True,
                "service": "vocec-mock-mlx-backend",
                "backend": "mock",
                "version": 1,
            },
        )

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/render-chunk":
            self._send_json(404, {"ok": False, "success": False, "error": "Not found"})
            return

        started = time.perf_counter()
        try:
            payload = self._read_json_body()
            response = self._render_chunk(payload, started)
        except JsonError as exc:
            self._send_json(exc.status, {"ok": False, "success": False, "error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - defensive server boundary
            logger.exception("render failed")
            self._send_json(500, {"ok": False, "success": False, "error": str(exc)})
            return

        self._send_json(200, response)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json_body(self) -> dict[str, Any]:
        content_length = self.headers.get("Content-Length")
        try:
            size = int(content_length or "0")
        except ValueError as exc:
            raise JsonError("Invalid Content-Length header") from exc
        if size <= 0:
            raise JsonError("Request body is required")
        if size > MAX_REQUEST_BYTES:
            raise JsonError("Request body is too large", status=413)

        raw_body = self.rfile.read(size)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JsonError("Request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise JsonError("Request JSON must be an object")
        return payload

    def _render_chunk(self, payload: dict[str, Any], started: float) -> dict[str, Any]:
        output_path = _required_string(payload, "output_path")
        text = _required_string(payload, "text")
        chunk_id = _optional_string(payload, "chunk_id", "chunk_0000")
        audio_format = _optional_string(payload, "format", "wav").lower()
        if audio_format != "wav":
            raise JsonError("Only wav output is supported by the mock backend")

        sample_rate = _optional_int(payload, "sample_rate", DEFAULT_SAMPLE_RATE, minimum=8000)
        duration_seconds = _optional_float(
            payload,
            "duration_seconds",
            DEFAULT_DURATION_SECONDS,
            minimum=0.1,
            maximum=10.0,
        )
        target = Path(output_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)

        _write_tone_wav(target, sample_rate=sample_rate, duration_seconds=duration_seconds)
        elapsed = time.perf_counter() - started
        logger.info(
            "rendered chunk_id=%s text_chars=%d output_path=%s elapsed=%.3fs",
            chunk_id,
            len(text),
            target,
            elapsed,
        )
        return {
            "ok": True,
            "success": True,
            "chunk_id": chunk_id,
            "output_path": str(target),
            "duration_seconds": duration_seconds,
            "sample_rate": sample_rate,
            "format": "wav",
            "bytes_written": target.stat().st_size,
        }

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise JsonError(f"Missing or invalid field: {key}")
    return value.strip()


def _optional_string(payload: dict[str, Any], key: str, default: str) -> str:
    value = payload.get(key, default)
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise JsonError(f"Invalid field: {key}")
    return value.strip() or default


def _optional_int(payload: dict[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = payload.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise JsonError(f"Invalid field: {key}") from exc
    if parsed < minimum:
        raise JsonError(f"Field {key} must be at least {minimum}")
    return parsed


def _optional_float(
    payload: dict[str, Any],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = payload.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise JsonError(f"Invalid field: {key}") from exc
    if parsed < minimum or parsed > maximum:
        raise JsonError(f"Field {key} must be between {minimum} and {maximum}")
    return parsed


def _write_tone_wav(path: Path, *, sample_rate: int, duration_seconds: float) -> None:
    frame_count = max(1, int(sample_rate * duration_seconds))
    amplitude = 0.12
    frequency = 440.0
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        for frame in range(frame_count):
            sample = int(32767 * amplitude * math.sin(2.0 * math.pi * frequency * frame / sample_rate))
            wav_file.writeframesraw(struct.pack("<h", sample))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Vocec MLX Local mock backend.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host, localhost only.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in ALLOWED_BIND_HOSTS:
        raise SystemExit("Refusing to bind non-local host. Use 127.0.0.1, localhost, or ::1.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((args.host, args.port), VocecMockHandler)
    logger.info("Vocec mock MLX backend listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Vocec mock MLX backend")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
