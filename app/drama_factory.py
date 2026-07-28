from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from .cancel import CancelledTask, is_cancel_requested, run_cancellable
from .video_utils import ffmpeg_bin, get_video_info, safe_stem
from .visual_variant import EXPORT_FPS, EXPORT_HEIGHT, EXPORT_WIDTH, _mp4_compat_args


ANGLE_PROFILES: list[dict[str, str]] = [
    {
        "id": "betrayal",
        "name": "Betrayal",
        "hook": "She found out the truth.",
        "title": "She finally saw his betrayal",
        "cta": "Would you forgive him?",
    },
    {
        "id": "revenge",
        "name": "Revenge",
        "hook": "Now it is her turn.",
        "title": "Her revenge starts here",
        "cta": "What should she do next?",
    },
    {
        "id": "billionaire",
        "name": "Billionaire",
        "hook": "Nobody knew who he really was.",
        "title": "The billionaire secret changed everything",
        "cta": "Did you expect that twist?",
    },
    {
        "id": "pregnancy",
        "name": "Pregnancy",
        "hook": "One secret changed her life.",
        "title": "The pregnancy secret she hid",
        "cta": "Should she tell the truth?",
    },
    {
        "id": "identity",
        "name": "Identity Reveal",
        "hook": "Her real identity shocked them all.",
        "title": "They humiliated the wrong woman",
        "cta": "Who deserves the ending?",
    },
]


EMOTION_KEYWORDS: dict[str, list[str]] = {
    "cheating": ["cheat", "affair", "mistress", "lover", "other woman", "出轨", "小三", "情人", "背叛"],
    "betrayal": ["betray", "lied", "lie", "deceive", "betrayal", "背叛", "欺骗", "骗我"],
    "pregnancy": ["pregnant", "baby", "child", "abortion", "怀孕", "孩子", "宝宝", "流产"],
    "billionaire": ["billionaire", "ceo", "rich", "heir", "president", "总裁", "豪门", "富豪", "继承人"],
    "revenge": ["revenge", "payback", "regret", "destroy", "复仇", "报复", "后悔", "代价"],
    "divorce": ["divorce", "wife", "husband", "marriage", "离婚", "妻子", "丈夫", "婚姻"],
    "secret_identity": ["secret", "identity", "real name", "hidden", "秘密", "身份", "真实身份", "隐藏"],
    "humiliation": ["humiliate", "kneel", "shame", "insult", "羞辱", "下跪", "看不起", "侮辱"],
    "confrontation": ["why", "how dare", "stop", "confront", "为什么", "你敢", "住手", "对质"],
}


@dataclass
class DramaFactoryOptions:
    max_clips: int = 20
    target_seconds: float = 30
    flex_seconds: float = 5
    min_seconds: float = 25
    max_seconds: float = 35
    versions_per_clip: int = 3
    whisper_model: str = "base"
    keywords: tuple[str, ...] = ()


def _run(command: list[str], *, task_id: str | None = None) -> subprocess.CompletedProcess[str]:
    result = run_cancellable(command, task_id=task_id)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"FFmpeg failed, code={result.returncode}: {stderr[-1600:]}")
    return result


def _safe_drawtext(text: str, limit: int = 54) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())[:limit]
    return cleaned.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


def _extract_audio(input_video: Path, work_dir: Path, task_id: str) -> Path:
    audio_path = work_dir / "audio.wav"
    _run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(input_video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(audio_path),
        ],
        task_id=task_id,
    )
    return audio_path


def _load_sidecar_transcript(input_video: Path) -> list[dict[str, Any]]:
    for suffix in (".json", ".srt", ".vtt"):
        candidate = input_video.with_suffix(suffix)
        if not candidate.exists():
            continue
        if suffix == ".json":
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return [_clean_segment(item) for item in payload if isinstance(item, dict)]
        return _parse_subtitle_text(candidate.read_text(encoding="utf-8", errors="ignore"))
    return []


def _parse_timecode(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        return float(value)
    except ValueError:
        return 0.0


def _parse_subtitle_text(content: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing = next((line for line in lines if "-->" in line), "")
        if not timing:
            continue
        left, right = [part.strip() for part in timing.split("-->", 1)]
        text_lines = [line for line in lines if line != timing and not line.isdigit()]
        text = " ".join(text_lines).strip()
        if text:
            segments.append({"start": _parse_timecode(left), "end": _parse_timecode(right), "text": text})
    return segments


def _clean_segment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": float(item.get("start") or 0),
        "end": float(item.get("end") or item.get("start") or 0),
        "text": str(item.get("text") or "").strip(),
    }


def _transcribe(audio_path: Path, input_video: Path, options: DramaFactoryOptions) -> tuple[list[dict[str, Any]], str]:
    sidecar = _load_sidecar_transcript(input_video)
    if sidecar:
        return sidecar, "sidecar"

    try:
        import whisper  # type: ignore
    except Exception:
        return [], "missing_whisper"

    model = whisper.load_model(options.whisper_model)
    result = model.transcribe(str(audio_path), verbose=False)
    segments = [_clean_segment(item) for item in result.get("segments", [])]
    return segments, f"whisper:{options.whisper_model}"


def _keyword_hits(text: str, custom_keywords: tuple[str, ...] = ()) -> list[str]:
    lowered = text.lower()
    hits = []
    for tag, keywords in EMOTION_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            hits.append(tag)
    custom_hits = [keyword for keyword in custom_keywords if keyword and keyword.lower() in lowered]
    if custom_hits:
        hits.append("custom_keywords")
    return hits


def _detect_high_priority_segments(
    transcript: list[dict[str, Any]],
    duration: float,
    options: DramaFactoryOptions,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript):
        tags = _keyword_hits(segment["text"], options.keywords)
        if not tags:
            continue
        peak = float(segment["start"])
        desired = max(options.min_seconds, min(options.target_seconds, options.max_seconds))
        window_start = max(0.0, peak - desired * 0.36)
        window_end = min(duration, window_start + desired)
        if window_end - window_start < options.min_seconds:
            window_start = max(0.0, window_end - options.min_seconds)
        nearby_text = " ".join(item["text"] for item in transcript[max(0, index - 2) : index + 4])
        candidates.append(
            {
                "start": round(window_start, 3),
                "end": round(window_end, 3),
                "peak": round(peak, 3),
                "text": nearby_text.strip() or segment["text"],
                "emotion_tags": sorted(set(tags)),
                "priority": "HIGH_PRIORITY_CLIP",
                "score": len(tags) * 10 + min(8, len(nearby_text) // 24),
            }
        )
    return _dedupe_candidates(candidates)


def _fallback_candidates(duration: float, options: DramaFactoryOptions) -> list[dict[str, Any]]:
    if duration <= 0:
        return []
    clip_len = max(options.min_seconds, min(options.max_seconds, options.target_seconds, duration))
    spacing = max(clip_len, duration / max(options.max_clips, 1))
    candidates = []
    cursor = 0.0
    while cursor < duration and len(candidates) < options.max_clips:
        end = min(duration, cursor + clip_len)
        candidates.append(
            {
                "start": round(cursor, 3),
                "end": round(end, 3),
                "peak": round(cursor + (end - cursor) / 2, 3),
                "text": "",
                "emotion_tags": ["fallback_sample"],
                "priority": "FALLBACK_SAMPLE",
                "score": 1,
            }
        )
        cursor += spacing
    return candidates


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates.sort(key=lambda item: (item["start"], item["end"]))
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        if results and candidate["start"] < results[-1]["end"] - 4:
            merged = results[-1]
            merged["end"] = max(merged["end"], candidate["end"])
            merged["emotion_tags"] = sorted(set(merged["emotion_tags"]) | set(candidate["emotion_tags"]))
            merged["text"] = (merged.get("text", "") + " " + candidate.get("text", "")).strip()
        else:
            results.append(candidate)
    return results


def _clip_candidates(candidates: list[dict[str, Any]], options: DramaFactoryOptions, duration: float) -> list[dict[str, Any]]:
    clipped = []
    ranked = sorted(candidates, key=lambda item: (float(item.get("score") or 0), float(item.get("peak") or 0)), reverse=True)
    ranked = _dedupe_candidates(ranked)
    for candidate in ranked[: max(1, options.max_clips)]:
        peak = float(candidate.get("peak") or candidate["start"])
        desired = max(options.min_seconds, min(options.max_seconds, options.target_seconds, duration))
        start = max(0.0, peak - desired * 0.36)
        end = min(duration, start + desired)
        if end - start < options.min_seconds:
            start = max(0.0, end - options.min_seconds)
        if end - start >= 3:
            clipped.append({**candidate, "start": round(start, 3), "end": round(end, 3)})
    return clipped


def _rewrite_subtitles(text: str, angle: dict[str, str], tags: list[str]) -> list[str]:
    source = re.sub(r"\s+", " ", text.strip())
    if source:
        sentences = re.split(r"(?<=[.!?。！？])\s+", source)
        lines = [sentence.strip() for sentence in sentences if sentence.strip()][:3]
    else:
        lines = []
    if not lines:
        lines = [
            "She stayed silent for too long.",
            "Then one moment exposed everything.",
            "And nobody was ready for what came next.",
        ]
    tag_text = ", ".join(tags[:2]) if tags else angle["name"].lower()
    return [
        f"{angle['hook']}",
        f"{lines[0][:64]}",
        f"This {tag_text} moment changes everything.",
    ]


def _script_for_clip(candidate: dict[str, Any], angle: dict[str, str], clip_index: int) -> dict[str, Any]:
    tags = list(candidate.get("emotion_tags") or [])
    subtitles = _rewrite_subtitles(str(candidate.get("text") or ""), angle, tags)
    return {
        "angle": angle["id"],
        "hook": angle["hook"],
        "title": angle["title"],
        "cta": angle["cta"],
        "subtitles": subtitles,
        "cliffhanger": "End before the reaction resolves.",
        "clip_index": clip_index,
        "emotion_tags": tags,
    }


def _render_version(
    input_video: Path,
    output_path: Path,
    candidate: dict[str, Any],
    script: dict[str, Any],
    *,
    task_id: str,
    version_index: int,
) -> None:
    start = float(candidate["start"])
    duration = max(1.0, float(candidate["end"]) - start)
    variant_shift = (version_index - 1) % 10
    zoom = 1.012 + variant_shift * 0.003
    contrast = 1.015 + variant_shift * 0.004
    saturation = 1.02 + variant_shift * 0.006
    brightness = -0.006 + variant_shift * 0.0012
    border_alpha = 0.08 + (variant_shift % 3) * 0.025
    vf = [
        f"scale={EXPORT_WIDTH}:{EXPORT_HEIGHT}:force_original_aspect_ratio=increase",
        f"crop={EXPORT_WIDTH}:{EXPORT_HEIGHT}",
        f"fps={EXPORT_FPS}",
        "setsar=1",
        f"scale=iw*{zoom:.4f}:ih*{zoom:.4f}",
        f"crop={EXPORT_WIDTH}:{EXPORT_HEIGHT}",
        f"eq=contrast={contrast:.3f}:saturation={saturation:.3f}:brightness={brightness:.4f}",
        "noise=alls=2:allf=t+u",
        "vignette=PI/10",
        f"drawbox=x=0:y=0:w=iw:h=ih:color=white@{border_alpha:.3f}:t=6",
        "fade=t=in:st=0:d=0.18",
    ]
    command = [
        ffmpeg_bin(),
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(input_video),
        "-t",
        f"{duration:.3f}",
        "-vf",
        ",".join(vf),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-shortest",
        "-metadata",
        f"comment=drama-clip-{script['clip_index']}-v{version_index}",
        *_mp4_compat_args(),
        str(output_path),
    ]
    _run(command, task_id=task_id)


def render_drama_factory(
    *,
    input_video: Union[str, Path],
    output_dir: Union[str, Path],
    task_id: str,
    options: DramaFactoryOptions,
) -> tuple[list[Path], Path, dict[str, Any]]:
    if is_cancel_requested(task_id):
        raise CancelledTask("Task cancelled")

    input_path = Path(input_video)
    info = get_video_info(input_path)
    output_root = Path(output_dir) / safe_stem(input_path.stem)
    work_dir = output_root / f"{task_id}_work"
    output_root.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    audio_path = _extract_audio(input_path, work_dir, task_id)
    transcript, transcript_source = _transcribe(audio_path, input_path, options)
    candidates = _detect_high_priority_segments(transcript, info.duration, options)
    if not candidates:
        candidates = _fallback_candidates(info.duration, options)
    clips = _clip_candidates(candidates, options, info.duration)

    version_count = max(1, min(options.versions_per_clip, 10))
    outputs: list[Path] = []
    metadata: dict[str, Any] = {
        "source_video": str(input_path),
        "transcript_source": transcript_source,
        "video_info": info.model_dump() if hasattr(info, "model_dump") else info.dict(),
        "clips": [],
        "outputs": [],
    }

    for clip_index, candidate in enumerate(clips, start=1):
        clip_record = {
            "clip_index": clip_index,
            "start": candidate["start"],
            "end": candidate["end"],
            "priority": candidate["priority"],
            "emotion_tags": candidate["emotion_tags"],
            "versions": [],
        }
        for version_index in range(1, version_count + 1):
            angle = ANGLE_PROFILES[(version_index - 1) % len(ANGLE_PROFILES)]
            if is_cancel_requested(task_id):
                raise CancelledTask("Task cancelled")
            script = _script_for_clip(candidate, angle, clip_index)
            filename = f"clip_{clip_index:03d}_v{version_index:02d}.mp4"
            output_path = output_root / filename
            _render_version(input_path, output_path, candidate, script, task_id=task_id, version_index=version_index)
            outputs.append(output_path)
            item = {
                "file": str(output_path),
                "filename": filename,
                "hook": script["hook"],
                "title": script["title"],
                "cta": script["cta"],
                "clip_timestamps": {"start": candidate["start"], "end": candidate["end"]},
                "emotion_tags": script["emotion_tags"],
                "angle": script["angle"],
                "cliffhanger": script["cliffhanger"],
                "subtitles": script["subtitles"],
            }
            clip_record["versions"].append(item)
            metadata["outputs"].append(item)
        metadata["clips"].append(clip_record)

    metadata_path = output_root / "clips.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    return outputs, metadata_path, metadata


def options_from_tool_options(values: dict[str, Any]) -> DramaFactoryOptions:
    def number(name: str, default: float) -> float:
        try:
            return float(values.get(name, default))
        except (TypeError, ValueError):
            return default

    def integer(name: str, default: int) -> int:
        return int(max(1, number(name, default)))

    def keywords() -> tuple[str, ...]:
        raw = str(values.get("keywords") or "")
        items = [item.strip() for item in re.split(r"[\n,，、]+", raw) if item.strip()]
        return tuple(dict.fromkeys(items[:200]))

    target_seconds = max(10.0, min(number("target_seconds", 30), 90.0))
    flex_seconds = max(0.0, min(number("flex_seconds", 5), 20.0))
    min_seconds = max(5.0, target_seconds - flex_seconds)
    max_seconds = max(min_seconds + 1.0, target_seconds + flex_seconds)

    return DramaFactoryOptions(
        max_clips=min(100, integer("max_clips", 20)),
        target_seconds=target_seconds,
        flex_seconds=flex_seconds,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        versions_per_clip=min(10, integer("versions_per_clip", 3)),
        whisper_model=str(values.get("whisper_model") or "base"),
        keywords=keywords(),
    )
