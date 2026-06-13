# Vocec Mock MLX Backend

This mock backend is only for development plumbing tests. It is not real TTS, does not use MLX-Audio, and only proves that TTS-Story can call a local chunk renderer, receive progress, and find generated chunk WAV files.

## Start The Mock Backend

From the repo root:

```bash
python tools/vocec_mock_mlx_backend.py --host 127.0.0.1 --port 7860
```

Stop it with `Ctrl-C` in that terminal.

## Check Health

```bash
curl http://127.0.0.1:7860/health
```

Expected shape:

```json
{"ok":true,"success":true,"service":"vocec-mock-mlx-backend","backend":"mock","version":1}
```

## Render One Manual Chunk

Use a temporary local output path, not a repo fixture or committed audio file:

```bash
curl -X POST http://127.0.0.1:7860/v1/render-chunk \
  -H "Content-Type: application/json" \
  -d '{
    "project": "Vocec",
    "chunk_id": "chunk_0000",
    "text": "Short non-private test text.",
    "speaker": "narrator",
    "model": "mock",
    "voice": "narrator",
    "voice_sample_path": null,
    "output_path": "/tmp/vocec-mock/chunk_0000.wav",
    "format": "wav",
    "sample_rate": 24000,
    "speed": 1.0,
    "overwrite": true,
    "timeout_seconds": 180
  }'
```

Then confirm the WAV exists and is non-empty:

```bash
ls -lh /tmp/vocec-mock/chunk_0000.wav
```

## Point TTS-Story At The Mock

In TTS-Story settings:

- Default Engine: `Vocec MLX Local`
- Base URL: `http://127.0.0.1:7860`
- Audio Format: `wav`
- Model and voice can use harmless placeholder values, such as `mock` and `narrator`.
- Leave real voice sample paths out of tracked config. Use ignored local config or environment variables for private values.

Create a tiny non-private test job. After generation starts, open the job folder and verify `chunk_0000.wav` appears under the TTS-Story job chunk directory. The file will contain a simple test tone, not narrated speech.

## Privacy Notes

The mock logs only chunk id, text length, output path, and elapsed time. Do not send private manuscripts, real voice sample paths, or generated audiobook output when testing public-repo changes.
