"""Local HTTP adapter for Vocec MLX-Audio chunk rendering."""
from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
import soundfile as sf

from .base import EngineCapabilities, TtsEngineBase, VoiceAssignment

logger = logging.getLogger(__name__)

DEFAULT_VOCEC_MLX_BASE_URL = "http://127.0.0.1:7860"
DEFAULT_VOCEC_MLX_SAMPLE_RATE = 24000
DEFAULT_VOCEC_MLX_TIMEOUT_SECONDS = 180
DEFAULT_VOCEC_MLX_RETRY_COUNT = 1
DEFAULT_VOCEC_MLX_RETRY_BACKOFF_SECONDS = 2.0
VOICE_PROMPTS_DIR = Path("data/voice_prompts")


class VocecMLXLocalEngine(TtsEngineBase):
    """Adapter that asks a separate local MLX-Audio service to render chunks."""

    name = "vocec_mlx_local"
    capabilities = EngineCapabilities(
        supports_voice_cloning=True,
        supports_emotion_tags=False,
        supported_languages=None,
    )

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        default_voice: Optional[str] = None,
        default_voice_sample_path: Optional[str] = None,
        audio_format: str = "wav",
        sample_rate: int = DEFAULT_VOCEC_MLX_SAMPLE_RATE,
        default_speed: float = 1.0,
        timeout_seconds: int = DEFAULT_VOCEC_MLX_TIMEOUT_SECONDS,
        retry_count: int = DEFAULT_VOCEC_MLX_RETRY_COUNT,
        retry_backoff_seconds: float = DEFAULT_VOCEC_MLX_RETRY_BACKOFF_SECONDS,
        overwrite_existing: bool = False,
        project_name: str = "Vocec",
        device: str = "local-http",
    ):
        super().__init__(device=device)
        self.base_url = self._env_or_value(
            "VOCEC_MLX_BASE_URL",
            base_url,
            DEFAULT_VOCEC_MLX_BASE_URL,
        ).rstrip("/") + "/"
        self._validate_local_base_url(self.base_url)
        self.model = self._env_or_value("VOCEC_MLX_MODEL", model, "")
        self.default_voice = self._env_or_value("VOCEC_MLX_VOICE", default_voice, "")
        self.default_voice_sample_path = self._env_or_value(
            "VOCEC_SAMPLE_PATH",
            default_voice_sample_path,
            "",
            fallback_env="VOCEC_MLX_VOICE_SAMPLE_PATH",
        )
        self.audio_format = (audio_format or "wav").strip().lower()
        if self.audio_format != "wav":
            raise ValueError("Vocec MLX Local currently supports WAV chunk output only.")
        self._sample_rate = int(sample_rate or DEFAULT_VOCEC_MLX_SAMPLE_RATE)
        self.default_speed = float(default_speed or 1.0)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_VOCEC_MLX_TIMEOUT_SECONDS))
        self.retry_count = max(0, int(retry_count or 0))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds or 0.0))
        self.overwrite_existing = bool(overwrite_existing)
        self.project_name = project_name or "Vocec"
        self.session = requests.Session()
        self._health_checked = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def generate_audio(
        self,
        text: str,
        voice: Optional[str] = None,
        lang_code: Optional[str] = None,
        speed: float = 1.0,
        sample_rate: Optional[int] = None,
        audio_prompt_path: Optional[str] = None,
        fx_settings=None,
        **_kwargs,
    ) -> bytes:
        """Generate a preview clip and return WAV bytes."""
        assignment = VoiceAssignment(
            voice=voice or None,
            lang_code=lang_code,
            audio_prompt_path=audio_prompt_path,
            speed_override=speed,
        )
        with tempfile.TemporaryDirectory(prefix="vocec_mlx_preview_") as tmp_dir:
            output_path = Path(tmp_dir) / "preview.wav"
            self._render_chunk(
                chunk_text=text,
                speaker="preview",
                assignment=assignment,
                output_path=output_path,
                chunk_id="preview",
                order_index=0,
                speed=speed,
                sample_rate=sample_rate or self.sample_rate,
            )
            return output_path.read_bytes()

    def generate_batch(
        self,
        segments: List[Dict],
        voice_config: Dict[str, Dict],
        output_dir: Path,
        speed: float = 1.0,
        sample_rate: Optional[int] = None,
        progress_cb=None,
        chunk_cb=None,
        parallel_workers: int = 1,
        pause_cb=None,
        cancel_cb=None,
    ) -> List[str]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_root = output_dir.resolve()

        chunk_items = self._flatten_segments(segments, voice_config)
        files: List[Optional[str]] = [None] * len(chunk_items)
        effective_sample_rate = int(sample_rate or self.sample_rate)
        effective_speed = float(speed or self.default_speed)

        if parallel_workers and int(parallel_workers) > 1:
            logger.info("Vocec MLX Local renders sequentially in this first adapter version.")

        for item in chunk_items:
            if callable(cancel_cb) and cancel_cb():
                break
            if callable(pause_cb) and pause_cb():
                break

            order_index = item["order_index"]
            output_path = output_root / f"chunk_{order_index:04d}.wav"
            self._ensure_output_path(output_path, output_root)

            logger.info(
                "Vocec MLX Local rendering chunk %s/%s speaker=%s text_chars=%s",
                order_index + 1,
                len(chunk_items),
                item["speaker"] or "default",
                len(item["text"] or ""),
            )

            duration_seconds, actual_sample_rate = self._render_chunk(
                chunk_text=item["text"],
                speaker=item["speaker"],
                assignment=item["assignment"],
                output_path=output_path,
                chunk_id=f"chunk_{order_index:04d}",
                order_index=order_index,
                speed=float(item["assignment"].speed_override or effective_speed),
                sample_rate=effective_sample_rate,
            )
            if actual_sample_rate:
                self._sample_rate = int(actual_sample_rate)
            files[order_index] = str(output_path)

            if callable(progress_cb):
                progress_cb()
            if callable(chunk_cb):
                chunk_meta = {
                    "speaker": item["speaker"],
                    "text": item["text"],
                    "segment_index": item["segment_index"],
                    "chunk_index": item["chunk_index"],
                    "order_index": order_index,
                    "duration_seconds": duration_seconds,
                }
                chunk_cb(item["chunk_index"], chunk_meta, str(output_path))

        return [path for path in files if path]

    def cleanup(self) -> None:
        self.session.close()

    def _flatten_segments(self, segments: List[Dict], voice_config: Dict[str, Dict]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for seg_idx, segment in enumerate(segments or []):
            speaker = segment.get("speaker") or "default"
            assignment = self._voice_assignment_for(voice_config, speaker)
            for chunk_idx, chunk_text in enumerate(segment.get("chunks") or []):
                items.append({
                    "speaker": speaker,
                    "text": chunk_text,
                    "segment_index": seg_idx,
                    "chunk_index": chunk_idx,
                    "order_index": len(items),
                    "assignment": assignment,
                })
        return items

    def _voice_assignment_for(self, voice_config: Dict[str, Dict], speaker: str) -> VoiceAssignment:
        payload = (voice_config or {}).get(speaker) or (voice_config or {}).get("default") or {}
        return VoiceAssignment(
            voice=payload.get("voice"),
            lang_code=payload.get("lang_code"),
            audio_prompt_path=payload.get("audio_prompt_path"),
            fx_payload=payload.get("fx"),
            speed_override=payload.get("speed"),
            extra=payload.get("extra") or {},
        )

    def _render_chunk(
        self,
        *,
        chunk_text: str,
        speaker: str,
        assignment: VoiceAssignment,
        output_path: Path,
        chunk_id: str,
        order_index: int,
        speed: float,
        sample_rate: int,
    ) -> Tuple[Optional[float], Optional[int]]:
        self._check_backend_health()
        self._prepare_output_path(output_path)
        voice_sample_path = self._resolve_voice_sample_path(assignment.audio_prompt_path)
        payload = {
            "project": self.project_name,
            "chunk_id": chunk_id,
            "text": chunk_text,
            "speaker": speaker or "default",
            "model": self.model,
            "voice": assignment.voice or self.default_voice,
            "voice_sample_path": voice_sample_path,
            "output_path": str(output_path),
            "format": self.audio_format,
            "sample_rate": int(sample_rate or self.sample_rate),
            "speed": float(speed or self.default_speed),
            "overwrite": bool(self.overwrite_existing),
            "timeout_seconds": self.timeout_seconds,
        }
        extra = assignment.extra or {}
        if extra:
            payload["extra"] = extra

        response = self._post_render_chunk(payload, order_index)
        self._write_response_audio_if_present(response, output_path)
        duration_seconds, actual_sample_rate = self._validate_audio_file(output_path, order_index)
        response_duration = response.get("duration_seconds")
        if response_duration is not None:
            try:
                duration_seconds = float(response_duration)
            except (TypeError, ValueError):
                pass
        response_sample_rate = response.get("sample_rate")
        if response_sample_rate is not None:
            try:
                actual_sample_rate = int(response_sample_rate)
            except (TypeError, ValueError):
                pass
        return duration_seconds, actual_sample_rate

    def _check_backend_health(self) -> None:
        if self._health_checked:
            return
        health_url = urljoin(self.base_url, "health")
        try:
            response = self.session.get(health_url, timeout=min(self.timeout_seconds, 10))
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if payload and (payload.get("ok") is False or payload.get("success") is False):
                raise RuntimeError(payload.get("error") or "health check reported failure")
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Vocec MLX Local server is not reachable at {self.base_url.rstrip('/')}. "
                "Start the local MLX-Audio backend and retry."
            ) from exc
        except RuntimeError as exc:
            raise RuntimeError(f"Vocec MLX Local health check failed: {exc}") from exc
        self._health_checked = True

    def _post_render_chunk(self, payload: Dict[str, Any], order_index: int) -> Dict[str, Any]:
        render_url = urljoin(self.base_url, "v1/render-chunk")
        last_error: Optional[BaseException] = None
        for attempt in range(self.retry_count + 1):
            try:
                response = self.session.post(render_url, json=payload, timeout=self.timeout_seconds)
                retryable_status = response.status_code in {429, 500, 502, 503, 504}
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if response.ok and data.get("ok", True) is not False and data.get("success", True) is not False:
                    return data
                error_text = data.get("error") or response.reason or "unknown error"
                retryable_payload = bool(data.get("retryable"))
                if not (retryable_status or retryable_payload):
                    raise RuntimeError(
                        f"Vocec MLX Local failed chunk {order_index}: {error_text}"
                    )
                last_error = RuntimeError(error_text)
            except requests.RequestException as exc:
                last_error = exc

            if attempt < self.retry_count:
                self._cleanup_partial_output(Path(payload["output_path"]))
                time.sleep(self.retry_backoff_seconds * (attempt + 1))

        if isinstance(last_error, requests.RequestException):
            raise RuntimeError(
                f"Vocec MLX Local server is not reachable at {self.base_url.rstrip('/')}. "
                "Start the local MLX-Audio backend and retry."
            ) from last_error
        raise RuntimeError(f"Vocec MLX Local failed chunk {order_index}: {last_error}")

    def _write_response_audio_if_present(self, response: Dict[str, Any], output_path: Path) -> None:
        audio_b64 = response.get("audio_base64")
        if not audio_b64:
            return
        try:
            output_path.write_bytes(base64.b64decode(audio_b64))
        except Exception as exc:
            raise RuntimeError("Vocec MLX Local returned audio bytes that could not be written.") from exc

    def _validate_audio_file(self, output_path: Path, order_index: int) -> Tuple[Optional[float], Optional[int]]:
        if not output_path.exists():
            raise RuntimeError(
                f"Vocec MLX Local returned success but no file was written for chunk {order_index}."
            )
        if output_path.stat().st_size < 256:
            raise RuntimeError(
                f"Vocec MLX Local wrote an invalid or empty audio file for chunk {order_index}."
            )
        try:
            info = sf.info(str(output_path))
            duration = (info.frames / float(info.samplerate)) if info.frames and info.samplerate else None
            return duration, int(info.samplerate) if info.samplerate else None
        except Exception as exc:
            raise RuntimeError(
                f"Vocec MLX Local wrote audio that could not be opened for chunk {order_index}."
            ) from exc

    def _prepare_output_path(self, output_path: Path) -> None:
        if output_path.exists():
            if not self.overwrite_existing:
                raise RuntimeError(
                    f"Refusing to overwrite existing Vocec MLX chunk file: {output_path.name}. "
                    "Enable overwrite behavior or clear the output folder."
                )
            output_path.unlink()

    def _cleanup_partial_output(self, output_path: Path) -> None:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("Unable to remove partial Vocec MLX output %s", output_path.name)

    def _resolve_voice_sample_path(self, assigned_path: Optional[str]) -> Optional[str]:
        raw_path = (assigned_path or self.default_voice_sample_path or "").strip()
        if not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        if candidate.is_file():
            return str(candidate)
        if not candidate.is_absolute():
            prompt_candidate = VOICE_PROMPTS_DIR / raw_path
            if prompt_candidate.is_file():
                return str(prompt_candidate)
            prompt_name_candidate = VOICE_PROMPTS_DIR / candidate.name
            if prompt_name_candidate.is_file():
                return str(prompt_name_candidate)
        raise FileNotFoundError(
            f"Vocec MLX Local voice sample was not found: {candidate.name or 'configured sample'}"
        )

    @staticmethod
    def _ensure_output_path(output_path: Path, output_root: Path) -> None:
        resolved = output_path.resolve()
        if resolved.parent != output_root:
            raise ValueError("Vocec MLX Local output path escaped the requested chunk directory.")

    @staticmethod
    def _env_or_value(
        env_name: str,
        value: Optional[str],
        default: str,
        *,
        fallback_env: Optional[str] = None,
    ) -> str:
        env_value = os.getenv(env_name)
        if not env_value and fallback_env:
            env_value = os.getenv(fallback_env)
        selected = env_value if env_value not in (None, "") else value
        return str(selected or default).strip()

    @staticmethod
    def _validate_local_base_url(base_url: str) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http":
            raise ValueError("Vocec MLX Local base URL must use http:// for the local bridge.")
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "Vocec MLX Local base URL must point to localhost, 127.0.0.1, or ::1."
            )
