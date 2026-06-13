# Vocec MLX-Audio Local Engine Plan

## Scope

This is an investigation and design note for adding a minimal Apple Silicon-native backend path to TTS-Story. The proposed backend is a local MLX-Audio renderer exposed to TTS-Story as a new engine adapter named `vocec_mlx_local`, displayed as `Vocec MLX Local` or `MLX-Audio Local`.

TTS-Story should remain the audiobook UI, job queue, chunk reviewer, audio library, merge/rebuild/export layer, and text-prep workflow. MLX-Audio should be treated as a separate local service that renders one text chunk to one audio file.

No runtime adapter is implemented in this note.

## Current Architecture Findings

### Engine Implementations

TTS engines live under `src/engines/`.

Important files:

- `src/engines/base.py`: defines `TtsEngineBase`, `EngineCapabilities`, and `VoiceAssignment`.
- `src/tts_engine.py`: contains `EngineRegistry`, `AVAILABLE_ENGINES`, and `get_engine(...)`.
- `app.py`: owns higher-level engine creation through `_create_engine(...)`, config signatures through `_engine_signature(...)`, and shared engine caching through `get_tts_engine(...)`.

Existing engine adapters implement `generate_batch(...)` and usually write files named `chunk_0000.wav`, `chunk_0001.wav`, and so on into the `output_dir` that `app.py` provides.

### Audiobook Job Queue

The job queue is implemented in `app.py`.

Main pieces:

- `jobs`: in-memory job map.
- `job_queue`: `queue.Queue()` used by the worker thread.
- `process_job_worker()`: background worker loop.
- `process_audio_job(job_data)`: main audiobook generation pipeline.
- `data/jobs/jobs.db`: SQLite persistence for job state.
- `data/jobs/<job_id>/`: persisted job text/state storage.

The `/api/generate` route creates a job, persists the submitted text under `data/jobs/<job_id>/text.txt`, snapshots config and voice assignments, estimates chunk count, and queues the job.

### Text Chunking

Chunking is implemented by `src/text_processor.py` and selected in `app.py`.

Main pieces:

- `TextProcessor.process_text(...)`: parses speaker-tagged text into segments, then chunks each segment.
- `TextProcessor.chunk_text(...)`: delegates to word-based or character-based chunking.
- `app.py` `_create_text_processor_for_engine(...)`: chooses engine-specific chunk strategy and size.
- `app.py` `estimate_total_chunks(...)`: uses the same processor to estimate progress before queueing.

Most current local engines use character chunks with per-engine settings such as `kokoro_chunk_size`, `voxcpm_chunk_size`, `qwen3_chunk_size`, and `index_tts_chunk_size`.

### Chunk Audio Write Location

`app.py` creates job output folders under:

- `static/audio/<job_id>/`
- `static/audio/<job_id>/chunks/` for single-section jobs.
- `static/audio/<job_id>/chapter_XX/chunks/` for chapter jobs.
- `static/audio/<job_id>/book_XX/chapter_XX/chunks/` for book-mode jobs.

`process_audio_job(...)` calls an inner `generate_chunks(...)` helper. That helper passes the target `output_dir` to `engine.generate_batch(...)`.

The engine is expected to write chunk files directly into that `output_dir`. Existing engines generally write WAV files named `chunk_NNNN.wav`, return the generated file paths, and optionally call `chunk_cb(...)` for each completed chunk.

### Progress, ETA, Chunk Status, Preview, Playback, Export

The UI gets progress and job state from Flask endpoints in `app.py`:

- `/api/queue`: returns job status, progress, total/processed chunks, ETA, post-process progress, and review flags.
- `/api/jobs/<job_id>/chunks`: returns live chunk-review metadata for review-enabled jobs.
- `/api/library`: scans `static/audio/<job_id>/metadata.json`, `review_manifest.json`, and `chunks_metadata.json`.
- `/api/library/<job_id>/chunks`: returns completed-library chunk metadata and file URLs.
- `/api/preview`: renders a short preview clip for supported engines.
- `/api/download/<job_id>`: downloads the selected final output.
- `/api/download/<job_id>/zip`: downloads chapter outputs.
- `/api/download/<job_id>/m4b`: builds an M4B export with chapter markers.

The frontend files that consume these endpoints are mainly:

- `static/js/main.js`: generation UI, engine selection, voice assignments, preview calls.
- `static/js/queue.js`: active queue and live chunk review.
- `static/js/library.js`: completed library, playback, chunk review/regeneration, rebuild/export actions.
- `static/js/settings.js`: settings load/save and engine-specific settings panels.
- `templates/index.html`: engine options, settings controls, and generation controls.

### New Engine Contract

A new engine must satisfy:

- Register an engine key in `src/tts_engine.py` `EngineRegistry`.
- Export/import the adapter from `src/engines/__init__.py` if following current convention.
- Implement `TtsEngineBase`.
- Provide a `sample_rate` property.
- Implement `generate_batch(...)` with the common signature:

```python
def generate_batch(
    self,
    segments: list[dict],
    voice_config: dict[str, dict],
    output_dir: Path,
    speed: float = 1.0,
    sample_rate: int | None = None,
    progress_cb=None,
    chunk_cb=None,
    parallel_workers: int = 1,
) -> list[str]:
    ...
```

Recommended optional parameters for this adapter:

- `cancel_cb=None`
- `pause_cb=None`

`app.py` already detects optional parameters by inspecting the `generate_batch(...)` signature.

Adapter behavior should be:

- Create `output_dir` if needed.
- Flatten `segments[*].chunks` into chronological chunk tasks.
- For each chunk, render one output WAV file to `output_dir / f"chunk_{order_index:04d}.wav"`.
- Return paths in playback order.
- Call `progress_cb()` exactly once per completed chunk.
- Call `chunk_cb(chunk_idx, chunk_meta, str(output_path))` after the file exists.
- Raise exceptions on failed chunks so `process_audio_job(...)` marks the job failed.
- Implement `cleanup()` as a no-op for HTTP, or close persistent clients/sessions if used.

`chunk_meta` should include at least:

```python
{
    "speaker": speaker,
    "text": chunk_text,
    "segment_index": seg_idx,
    "chunk_index": chunk_idx,
    "order_index": order_index,
}
```

## Proposed Minimal Adapter Design

Use a local HTTP bridge as the first implementation.

Add a new adapter file:

- `src/engines/mlx_audio_local_engine.py`

Engine identity:

- internal key: `vocec_mlx_local`
- display name: `Vocec MLX Local` or `MLX-Audio Local`

Capabilities:

```python
EngineCapabilities(
    supports_voice_cloning=True,
    supports_emotion_tags=False,
    supported_languages=None,
)
```

The adapter should call a local MLX-Audio service per chunk. TTS-Story will provide text, voice assignment, speed, target sample rate, and an output path. The MLX service will render directly to the requested output path or return audio bytes that the adapter writes to the requested output path.

For the smallest reliable path, prefer a server response that confirms the written file:

- TTS-Story owns output path naming.
- The local service writes the audio file at that path.
- The adapter validates the file exists and has non-trivial size before firing callbacks.

If the local server cannot safely write to arbitrary paths, use a bytes response or temporary file response instead:

- The adapter sends chunk text and voice options.
- The server returns WAV bytes or a local temporary result path.
- The adapter writes/copies the result to TTS-Story's expected `output_path`.

## HTTP Request and Response Shape

Health/config validation:

```http
GET /health
```

Expected response:

```json
{
  "ok": true,
  "engine": "mlx-audio",
  "models": ["example-model"],
  "sample_rate": 24000,
  "formats": ["wav"]
}
```

Render one chunk:

```http
POST /v1/render-chunk
Content-Type: application/json
```

Request:

```json
{
  "project": "Vocec",
  "job_id": "local-job-id",
  "chunk_id": "chunk_0000",
  "text": "Text for exactly one chunk.",
  "speaker": "narrator",
  "model": "mlx-community/example-tts-model",
  "voice": "narrator",
  "voice_sample_path": "/path/to/local/voice/sample.wav",
  "output_path": "/path/to/local/tts-story/static/audio/job-id/chunks/chunk_0000.wav",
  "format": "wav",
  "sample_rate": 24000,
  "speed": 1.0,
  "overwrite": false,
  "timeout_seconds": 180
}
```

Response:

```json
{
  "ok": true,
  "output_path": "/path/to/local/tts-story/static/audio/job-id/chunks/chunk_0000.wav",
  "duration_seconds": 12.34,
  "sample_rate": 24000,
  "format": "wav",
  "model": "mlx-community/example-tts-model"
}
```

Failure response:

```json
{
  "ok": false,
  "error": "Model not loaded",
  "code": "MODEL_UNAVAILABLE",
  "retryable": false
}
```

The adapter should not log full manuscript text. It may log job ID, chunk index, text length, speaker, model, elapsed time, and error code.

## One-Chunk Render Flow

For each flattened chunk:

1. Resolve the speaker assignment from `voice_config[speaker]` or `voice_config["default"]`.
2. Resolve the effective voice sample from the assignment's `audio_prompt_path`, `voice`, or local Vocec config default.
3. Build `output_path = output_dir / f"chunk_{order_index:04d}.wav"`.
4. If `overwrite` is false and `output_path` exists, fail fast or skip only if resume semantics explicitly allow it. For first implementation, fail fast to avoid stale audio.
5. POST one render request to the local server.
6. Validate that `output_path` exists, is not empty, and can be opened by `soundfile` or `wave`.
7. Append the output path into the return list by chronological order.
8. Call `progress_cb()`.
9. Call `chunk_cb(...)`.
10. Check `pause_cb()` or `cancel_cb()` before starting the next chunk.

## Error Handling

Adapter errors should be explicit and actionable:

- Server unavailable: `RuntimeError("Vocec MLX Local server is not reachable at <base_url>. Start the local backend and retry.")`
- Health check failed: include returned code/message.
- Missing voice sample: `FileNotFoundError` with placeholder-style guidance, not private examples.
- Render timeout: `TimeoutError` or `RuntimeError` saying which chunk index timed out.
- Empty output file: `RuntimeError("MLX-Audio returned success but no valid audio file was written for chunk N.")`
- Unsupported format: fail during config validation before queueing if possible.

Do not retry non-retryable errors such as missing model, invalid sample path, invalid output path, or unsupported format.

## Timeout and Retry Behavior

Proposed defaults:

- `vocec_mlx_timeout_seconds`: `180`
- `vocec_mlx_retry_count`: `1`
- `vocec_mlx_retry_backoff_seconds`: `2`
- `vocec_mlx_parallel_chunks`: `1` for first implementation

Retry only for:

- connection reset
- HTTP `429`
- HTTP `500`, `502`, `503`, `504`
- response JSON with `"retryable": true`

Do not retry once a chunk output file has been partially written unless the adapter first deletes or overwrites that specific partial file according to `overwrite` behavior. For safety, the first implementation should render to `chunk_NNNN.tmp.wav`, validate it, then atomically replace/rename to `chunk_NNNN.wav` if the local service supports temp targets.

## Startup and Config Validation

The adapter constructor should validate cheap, non-private settings:

- `base_url` is present and starts with `http://127.0.0.1`, `http://localhost`, or another explicitly configured local URL.
- timeout and retry values are numeric and within sane bounds.
- format is `wav` for first implementation.
- output path handling stays inside TTS-Story's per-job output directory.

The first actual generation or an optional settings test endpoint should call `/health`.

Voice sample validation should happen before the first chunk:

- If `vocec_mlx_default_voice_sample_path` is configured, verify it exists locally.
- If per-speaker `audio_prompt_path` is set, verify it exists or can be resolved through the existing `data/voice_prompts` mechanism.
- Error messages must use generic labels and file names only where possible.

## Local Config Strategy

The repository currently uses `config.json` in the repo root, and `load_config()` only persists keys listed in `DEFAULT_CONFIG`. For a public fork and private Vocec workflow, do not commit real local paths or secrets.

Preferred future shape:

- Commit an example file with placeholders only, such as `config.vocec.example.json`.
- Keep the real local file ignored, such as `config.vocec.local.json`.
- Optionally support environment variable overrides:
  - `VOCEC_MLX_BASE_URL`
  - `VOCEC_MLX_MODEL`
  - `VOCEC_SAMPLE_PATH`
  - `VOCEC_OUTPUT_DIR`
  - `VOCEC_MLX_TIMEOUT_SECONDS`
  - `VOCEC_MLX_RETRY_COUNT`

Example committed config:

```json
{
  "project_name": "Vocec",
  "vocec_mlx_base_url": "http://127.0.0.1:7860",
  "vocec_mlx_model": "mlx-community/example-tts-model",
  "vocec_mlx_voice": "narrator",
  "vocec_mlx_voice_sample_path": "/path/to/local/voice/sample.wav",
  "vocec_mlx_output_dir": "/path/to/local/vocec/output",
  "vocec_mlx_audio_format": "wav",
  "vocec_mlx_speed": 1.0,
  "vocec_mlx_timeout_seconds": 180,
  "vocec_mlx_retry_count": 1,
  "vocec_mlx_chunk_size": 450,
  "vocec_mlx_keep_intermediate_chunks": true,
  "vocec_mlx_overwrite_existing": false
}
```

The adapter should not need `vocec_mlx_output_dir` for normal TTS-Story generation because `app.py` already supplies per-job chunk directories. Keep that option for standalone Vocec backend workflows only, or treat it as a local server setting outside TTS-Story.

## Proposed `.gitignore` Additions

Current `.gitignore` already covers many user-generated artifacts:

- `static/audio/*`
- `static/samples/*`
- `*.log`
- `.env`
- `models/`
- `data/voice_prompts/*`
- `data/jobs/`
- `data/chatterbox_voices.json`
- `data/custom_voices.json`
- `data/*.db`

Recommended additions:

```gitignore
# Local/private Vocec config
config.local.json
config.vocec.local.json
*.local.json

# Local/private Vocec inputs and outputs
vocec.local/
vocec-output/
manuscripts/
*.manuscript.txt

# Generated audio and local render scratch
*.wav
*.mp3
*.m4a
*.flac
*.ogg
*.aac
*.m4b
*.tmp.wav
```

Before adding broad audio ignores, verify they do not hide intentional tiny test fixtures. If future tests need audio fixtures, place them under a dedicated allowlisted folder such as `tests/fixtures/audio/README.md` with synthetic/generated samples only.

Repository hygiene follow-up:

- `config.json` is currently tracked. It can contain private settings and should be replaced with an example config plus an ignored local config.
- `jobs.db` is currently tracked at the repository root. Runtime job state should not be committed.
- Do not commit real voice samples, manuscripts, generated audio, logs, local database files, `.env`, or machine-specific paths.

## Privacy Boundaries

Never commit:

- real voice sample paths
- real voice sample audio
- manuscript text
- generated chunk or final audio
- logs
- `.env`
- local config with real paths or private URLs
- local job databases
- model caches

Committed files may contain only placeholders such as:

- `/path/to/local/voice/sample.wav`
- `/path/to/local/vocec/output`
- `VOCEC_SAMPLE_PATH`
- `VOCEC_OUTPUT_DIR`

The real local values should live only in ignored local config or shell environment variables.

## Local Paths Handling

Runtime behavior should be:

- Read a default sample path from ignored local config or `VOCEC_SAMPLE_PATH`.
- Allow per-speaker `audio_prompt_path` from the existing voice prompt UI where possible.
- Pass paths to the local MLX server only at runtime.
- Never persist private absolute paths into committed example files.
- Avoid writing private absolute paths into job metadata when possible. If current chunk metadata stores `file_path`, consider relying on `relative_file` for UI/library behavior and treating absolute `file_path` as runtime-only.

Existing chunk metadata currently stores both `file_path` and `relative_file`. The UI primarily uses `relative_file` to build `/static/audio/<job_id>/...` URLs. A future cleanup could remove or minimize absolute `file_path` persistence in `chunks_metadata.json`.

## Files Likely to Change for Minimal Implementation

Backend:

- `src/engines/mlx_audio_local_engine.py`: new adapter.
- `src/tts_engine.py`: import/register `vocec_mlx_local`.
- `src/engines/__init__.py`: export adapter by convention.
- `app.py`: add default config keys, normalize engine options, engine signature, `_create_engine(...)`, voice validation, chunk size selection, and possibly `/api/health` metadata.

Frontend:

- `templates/index.html`: add engine option and minimal settings panel or settings fields.
- `static/js/settings.js`: load/save local MLX settings and tab mapping.
- `static/js/main.js`: display name, local-mode indicator, prompt-engine behavior, voice assignment behavior, generation overrides if exposed.
- `static/js/queue.js`: include engine in prompt-based review controls if chunk regeneration supports voice sample selection.
- `static/js/library.js`: display name, regeneration engine dropdown, prompt-based voice selection behavior.

Config/docs:

- `config.vocec.example.json`: committed placeholders only.
- `.gitignore`: ignored local config and private artifact rules.
- `README.md` or a short docs page: optional user-facing setup after implementation.

## Bridge Options

### Local HTTP API Bridge

Best first integration.

Pros:

- Keeps MLX dependencies isolated from TTS-Story's Python environment.
- Fits Apple Silicon workflows where the backend may run in a separate venv.
- Easy health check and explicit error messages.
- The adapter can remain small and testable.
- Avoids importing MLX into the Flask process.
- Can later be reused by other tools.

Cons:

- Requires a separate local server process.
- Needs timeout/retry handling.
- Must carefully validate local-only URL and output paths.

### CLI Bridge

Pros:

- Simple backend contract: run a command per chunk.
- No persistent server lifecycle.
- Easy to prototype with an existing MLX-Audio command.

Cons:

- Process startup per chunk may be slow.
- Harder to stream progress or distinguish retryable failures.
- Quoting/path handling is more fragile.
- Long books can spawn many subprocesses unless a batch mode is added.

### Direct Python Import

Pros:

- Lowest IPC overhead.
- No separate server to start.
- Direct access to model APIs and memory.

Cons:

- Couples TTS-Story to MLX dependencies and Python version constraints.
- Increases risk of dependency conflicts with existing engines.
- Loads MLX/model state into the Flask worker process.
- Harder to keep optional and Apple Silicon-specific.

Recommendation: start with a local HTTP API bridge. Keep the adapter generic enough that a CLI bridge can be added later if the MLX-Audio server path proves awkward.

## Quality Baseline: Chatterbox Regular

`Vocec MLX Local` remains the first implementation target for this plan, but the architecture should not assume MLX-Audio is the only future local backend. TTS-Story's engine interface should treat local engines as interchangeable chunk renderers: each engine receives normalized text chunks, voice assignment data, output paths, speed/config values, and callbacks, then writes one chunk audio file at a time.

Manual local testing before this integration found that the best-sounding local result came from regular Chatterbox with a local reference voice sample and custom inference settings. That result was regular Chatterbox, not Chatterbox Turbo. Chatterbox Turbo was faster but unstable in this workflow: it sometimes mangled simple words, produced long silences, or became unreliable after a chunk. Mimika/Qwen was opaque and slow for long jobs, and did not expose chunk files during generation. Kokoro and Orpheus preset voices were fast and easy to audition, but did not meet the quality bar for male long-form narration.

Because of that baseline, a future engine or engine profile called `Chatterbox Regular Local` should remain possible. The minimal MLX adapter should not bake in assumptions that only one local server, one request schema, or one model family can ever render chunks. Shared TTS-Story expectations should stay at the chunk-renderer boundary:

- input is one normalized chunk plus speaker/voice/config metadata
- output is one validated audio file in the path TTS-Story requested
- progress is reported per completed chunk
- pause/cancel/resume behavior remains owned by TTS-Story's job system
- review, rebuild, stitch/export, and library playback remain engine-agnostic

The system should support a quality-first engine that is slower than MLX-Audio or Chatterbox Turbo if the surrounding workflow makes it practical. The practical requirements are:

- parallel chunk rendering when the backend can safely support it
- resumability from the last completed chunk
- immediate per-chunk file visibility during generation
- overnight rendering without losing progress
- clear retry/failure behavior at chunk boundaries
- stable chunk metadata for review and rebuild

A future `Chatterbox Regular Local` adapter should support local reference voice samples and tunable inference settings, including:

- seed
- exaggeration
- cfg
- temperature
- min-p
- top-p
- repetition penalty

Do not commit or hard-code exact Chatterbox values in this repository yet. The exact proven settings should be supplied later from the older local workflow and stored only through placeholder example config, ignored local config, or environment variables as appropriate.

## Test Plan

Design-only checks for this task:

- Confirm no runtime files are changed.
- Confirm the design note contains no real local/private paths.
- Confirm Git status only adds this docs file and leaves existing user changes alone.

Implementation tests before adapter merge:

- Unit-test adapter flattening of `segments` into ordered chunk requests.
- Unit-test config normalization and validation.
- Unit-test HTTP success path with a fake local server or mocked `requests.Session`.
- Unit-test retry behavior for retryable HTTP failures.
- Unit-test non-retry behavior for invalid config and missing sample path.
- Unit-test callback ordering: output file validation, then `progress_cb`, then `chunk_cb`.
- Unit-test cancellation/pause callback checks between chunks.

Manual checks before implementation:

- Start TTS-Story and confirm existing engines still appear.
- Generate a tiny Kokoro or current local-engine job to verify baseline queue/library behavior.
- Confirm `.gitignore` blocks generated audio, logs, private local config, and voice samples.

Manual checks after implementation:

- Start local MLX-Audio server and call `/health`.
- Select `Vocec MLX Local`.
- Generate a one-sentence, one-speaker review-mode job.
- Verify `static/audio/<job_id>/chunks/chunk_0000.wav` exists and plays.
- Verify `/api/queue` reaches 100 percent and shows ETA/progress while running.
- Verify `/api/library` shows the completed item and engine name.
- Open chunk review, play the chunk, regenerate one chunk, and rebuild the final output.
- Export/download MP3/WAV and M4B for a chapter-mode job.
- Stop the local server and confirm a clear job failure message.
- Try a missing voice sample and confirm no private path is exposed in committed docs/config.

Suggested lightweight commands:

```bash
python -m py_compile app.py src/tts_engine.py src/engines/base.py
python -m py_compile src/engines/mlx_audio_local_engine.py
```

If tests are added:

```bash
python -m pytest
```

## Rollback Plan

Because the minimal implementation should be additive, rollback is straightforward:

1. Remove the `vocec_mlx_local` option from frontend selects/maps.
2. Remove the `vocec_mlx_local` branch from `app.py` engine creation/config handling.
3. Remove the registry entry from `src/tts_engine.py`.
4. Remove `src/engines/mlx_audio_local_engine.py`.
5. Leave `.gitignore` privacy additions in place unless they block intentional fixtures.
6. Existing generated jobs from other engines remain valid because library metadata is engine-key tolerant.

## Clear Next Step Prompt

Implement the minimal `Vocec MLX Local` engine adapter using a local HTTP API bridge. Add the new `vocec_mlx_local` engine key, placeholder-only example config, ignored local config rules, and the smallest UI/settings changes needed to select it. Keep the adapter boundary generic so it does not block a later `Chatterbox Regular Local` chunk-rendering engine/profile with local reference samples and tunable inference settings. Do not commit real local paths, samples, manuscripts, generated audio, logs, `.env`, or local database files. Preserve TTS-Story's existing job queue, chunk review, library, merge, rebuild, and export behavior.
