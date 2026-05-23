# Short Drama Factory MVP

This branch adds a first-pass short drama factory mode to the existing local video tool.

## What It Does

- Adds `POST /api/drama-factory`
- Extracts audio with FFmpeg
- Uses a sidecar `.json`, `.srt`, or `.vtt` transcript when present
- Uses local Whisper when the `whisper` Python package is installed
- Detects high-emotion segments with drama keywords
- Generates 15-35 second clip candidates
- Renders A/B versions for betrayal, revenge, billionaire, pregnancy, and identity reveal angles
- Outputs 9:16 MP4 files with hook and subtitle text overlays
- Writes `metadata.json`
- Packages all generated videos and metadata into a ZIP download

## Current MVP Limits

- OpenAI script enhancement is represented by deterministic templates for now.
- If Whisper is not installed and no sidecar transcript exists, the pipeline falls back to evenly sampled candidate clips.
- Background music layering is not included in this first pass.
- Persistent retry queues are not included yet; tasks still use the existing in-memory executor.

## API Form Fields

- `file`: raw drama video
- `max_clips`: default `3`, maximum `10`
- `min_seconds`: default `15`
- `max_seconds`: default `35`
- `versions_per_clip`: default `5`, maximum `5`
- `whisper_model`: default `base`

