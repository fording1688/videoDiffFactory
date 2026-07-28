from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Union

from PIL import Image, ImageDraw, ImageFont

from .ai_provider import AIProvider
from .cancel import CancelledTask, is_cancel_requested, run_cancellable
from .drama_prompts import COMBINED_REEL_PROMPT, DRAMA_HIGHLIGHT_PROMPT
from .video_utils import app_root, ffmpeg_bin, get_video_info, safe_stem
from .visual_variant import EXPORT_FPS, EXPORT_HEIGHT, EXPORT_WIDTH, _mp4_compat_args


SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
DEFAULT_HASHTAGS = ["#Drama", "#Reels", "#ShortDrama"]
_DRAWTEXT_SUPPORTED: bool | None = None
HIGHLIGHT_TYPES = [
    "打脸",
    "身份反转",
    "离婚",
    "求婚",
    "复仇",
    "背叛",
    "男主/女主出现",
    "霸总/富豪身份曝光",
    "冲突升级",
    "情绪爆发",
    "下跪",
    "威胁",
    "误会",
    "反转",
    "悬念结尾",
    "评论诱发点",
]
TYPE_KEYWORDS: dict[str, list[str]] = {
    "打脸": ["羞辱", "看不起", "废物", "打脸", "laughed", "humiliate", "wrong man"],
    "身份反转": ["身份", "真相", "原来", "竟然是", "real identity", "who he really was"],
    "离婚": ["离婚", "签字", "divorce", "wife", "husband"],
    "求婚": ["嫁给我", "戒指", "marry me", "proposal"],
    "复仇": ["复仇", "报复", "代价", "后悔", "revenge", "payback"],
    "背叛": ["背叛", "出轨", "小三", "betray", "cheat", "affair"],
    "男主/女主出现": ["他来了", "她来了", "少爷", "夫人", "男主", "女主"],
    "霸总/富豪身份曝光": ["总裁", "富豪", "继承人", "董事长", "billionaire", "ceo", "heir"],
    "冲突升级": ["你敢", "住手", "凭什么", "滚", "how dare", "stop"],
    "情绪爆发": ["为什么", "我恨你", "够了", "哭", "shut up", "enough"],
    "下跪": ["跪", "下跪", "kneel"],
    "威胁": ["威胁", "毁了", "杀", "threat", "destroy"],
    "误会": ["误会", "解释", "不是这样的", "misunderstand"],
    "反转": ["没想到", "其实", "原来", "turns out", "twist"],
    "悬念结尾": ["等等", "真相是", "你知道", "wait", "truth"],
    "评论诱发点": ["你觉得", "该不该", "原谅", "comment", "forgive"],
}


@dataclass
class DramaReelOptions:
    max_episodes: int = 10
    max_reels: int = 20
    target_seconds: float = 30
    min_seconds: float = 18
    max_seconds: float = 55
    frame_mode: str = "blur"
    burn_subtitles: bool = True
    subtitle_mode: str = "english"
    generate_videos: bool = False
    cookies_browser: str = ""
    ai_model: str = ""


def analyze_drama_reels(
    *,
    episode_paths: list[Union[str, Path]],
    output_dir: Union[str, Path],
    task_id: str,
    options: DramaReelOptions,
) -> tuple[list[Path], Path, dict[str, Any]]:
    if is_cancel_requested(task_id):
        raise CancelledTask("Task cancelled")

    episodes = discover_episodes(episode_paths, options.max_episodes)
    if not episodes:
        raise RuntimeError("没有找到可分析的视频文件。")

    root = Path(output_dir) / "drama_reels" / task_id
    subtitles_dir = root / "output" / "subtitles"
    reports_dir = root / "output" / "reports"
    reels_dir = root / "output" / "reels"
    for directory in (subtitles_dir, reports_dir, reels_dir):
        directory.mkdir(parents=True, exist_ok=True)

    provider = AIProvider(app_root(), model=options.ai_model or None)
    all_highlights: list[dict[str, Any]] = []
    episode_records: list[dict[str, Any]] = []
    for episode in episodes:
        if is_cancel_requested(task_id):
            raise CancelledTask("Task cancelled")
        info = get_video_info(episode["path"])
        segments, source = transcribe_episode(episode["path"], episode["episode"], subtitles_dir, task_id)
        render_segments = subtitle_segments_for_mode(provider, segments, episode["episode"], subtitles_dir, options, source)
        highlights = analyze_episode_highlights(
            provider=provider,
            episode=episode["episode"],
            video_path=episode["path"],
            segments=segments,
            duration=info.duration,
            options=options,
        )
        for item_index, item in enumerate(highlights, start=1):
            item["id"] = f"reel_e{episode['episode']:02d}_{item_index:03d}"
            item["episode"] = episode["episode"]
            item["source_file"] = str(episode["path"])
            item["subtitle_segments"] = reel_subtitle_segments(render_segments, item, options)
            item["subtitle_status"] = "ready" if item["subtitle_segments"] else "missing_real_subtitles"
            item["ffmpeg_command"] = single_clip_command_preview(episode["path"], item, reels_dir)
        all_highlights.extend(highlights)
        episode_records.append(
            {
                "episode": episode["episode"],
                "filename": episode["path"].name,
                "path": str(episode["path"]),
                "video_info": info.model_dump() if hasattr(info, "model_dump") else info.dict(),
                "subtitle_source": source,
                "burn_subtitle_source": "translated" if render_segments is not segments else source,
                "subtitle_json": str(subtitles_dir / f"episode_{episode['episode']:02d}.json"),
                "subtitle_srt": str(subtitles_dir / f"episode_{episode['episode']:02d}.srt"),
                "highlight_count": len(highlights),
            }
        )

    ranked = sorted(all_highlights, key=lambda item: float(item.get("overall_score") or 0), reverse=True)
    top20 = ranked[: max(1, min(options.max_reels, 20))]
    combined = suggest_combined_reels(provider, ranked[:40], options)
    plan = {
        "task_id": task_id,
        "provider": provider.config.provider,
        "model": provider.config.model,
        "episodes": episode_records,
        "single_reels": ranked,
        "combined_reels": combined,
        "output_dirs": {
            "reels": str(reels_dir),
            "subtitles": str(subtitles_dir),
            "reports": str(reports_dir),
        },
    }
    write_json(reports_dir / "highlights.json", {"episodes": episode_records, "highlights": ranked})
    write_json(reports_dir / "reel_plan.json", plan)
    write_json(reports_dir / "top20_reels.json", top20)

    generated: list[Path] = []
    if options.generate_videos:
        generated = generate_reels_from_plan(
            plan_path=reports_dir / "reel_plan.json",
            selected_ids=[item["id"] for item in top20],
            output_dir=reels_dir,
            options=options,
            task_id=task_id,
        )
    package_path = root / f"{task_id}_drama_reel_analyzer.zip"
    package_outputs(package_path, [reports_dir, subtitles_dir, reels_dir])
    metadata = {
        "operation": "drama_reel_analyzer",
        "episode_count": len(episodes),
        "highlight_count": len(ranked),
        "top20_count": len(top20),
        "combined_count": len(combined),
        "generated_count": len(generated),
        "reports_dir": str(reports_dir),
        "reels_dir": str(reels_dir),
        "subtitles_dir": str(subtitles_dir),
        "package_path": str(package_path),
        "top20_path": str(reports_dir / "top20_reels.json"),
        "reel_plan_path": str(reports_dir / "reel_plan.json"),
        "highlights_path": str(reports_dir / "highlights.json"),
        "ai_provider": provider.config.provider,
        "ai_model": provider.config.model,
        "ai_enabled": provider.available(),
    }
    return generated, reports_dir / "reel_plan.json", metadata


def discover_episodes(paths: Iterable[Union[str, Path]], max_episodes: int) -> list[dict[str, Any]]:
    videos: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            videos.extend(child for child in path.iterdir() if child.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES)
        elif path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES:
            videos.append(path)
    seen: set[Path] = set()
    records = []
    for path in videos:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        records.append({"episode": episode_number(path.name), "path": resolved})
    records.sort(key=lambda item: (item["episode"], natural_key(item["path"].name)))
    return records[: max(1, min(max_episodes, 50))]


def episode_number(filename: str) -> int:
    patterns = [
        r"episode[_\-\s]*(\d+)",
        r"\bep[_\-\s]*(\d+)",
        r"第\s*(\d+)\s*[集话]",
        r"[_\-\s](\d{1,3})(?=\D|$)",
    ]
    lowered = filename.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered, flags=re.I)
        if match:
            return max(1, int(match.group(1)))
    numbers = re.findall(r"\d+", filename)
    return max(1, int(numbers[0])) if numbers else 1


def natural_key(text: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def transcribe_episode(video_path: Path, episode: int, subtitles_dir: Path, task_id: str) -> tuple[list[dict[str, Any]], str]:
    sidecar = load_sidecar_subtitles(video_path)
    if sidecar:
        write_subtitle_outputs(episode, sidecar, subtitles_dir)
        return sidecar, "sidecar"
    audio_path = subtitles_dir / f"episode_{episode:02d}.wav"
    try:
        import faster_whisper  # type: ignore

        extract_audio_for_transcription(video_path, audio_path, task_id)
        model_name = os.getenv("DRAMA_WHISPER_MODEL", "base").strip() or "base"
        model = faster_whisper.WhisperModel(model_name, device="cpu", compute_type="int8")
        raw_segments, _ = model.transcribe(str(audio_path), vad_filter=True)
        segments = [
            {
                "start": seconds_to_timecode(float(item.start)),
                "end": seconds_to_timecode(float(item.end)),
                "text": str(item.text or "").strip(),
            }
            for item in raw_segments
            if str(item.text or "").strip()
        ]
        write_subtitle_outputs(episode, segments, subtitles_dir)
        audio_path.unlink(missing_ok=True)
        return segments, f"faster-whisper:{model_name}"
    except Exception:
        audio_path.unlink(missing_ok=True)

    try:
        import whisper  # type: ignore
    except Exception:
        segments = fallback_segments(video_path)
        write_subtitle_outputs(episode, segments, subtitles_dir)
        return segments, "placeholder:no_whisper"

    extract_audio_for_transcription(video_path, audio_path, task_id)
    model_name = os.getenv("DRAMA_WHISPER_MODEL", "base").strip() or "base"
    result = whisper.load_model(model_name).transcribe(str(audio_path), verbose=False)
    segments = [clean_segment(item) for item in result.get("segments", [])]
    write_subtitle_outputs(episode, segments, subtitles_dir)
    audio_path.unlink(missing_ok=True)
    return segments, f"whisper:{model_name}"


def extract_audio_for_transcription(video_path: Path, audio_path: Path, task_id: str) -> None:
    run_ffmpeg(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(audio_path),
        ],
        task_id,
    )


def load_sidecar_subtitles(video_path: Path) -> list[dict[str, Any]]:
    for suffix in (".json", ".srt", ".vtt"):
        candidate = video_path.with_suffix(suffix)
        if not candidate.exists():
            continue
        if suffix == ".json":
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            raw_segments = payload.get("segments", payload) if isinstance(payload, dict) else payload
            if isinstance(raw_segments, list):
                return [clean_segment(item) for item in raw_segments if isinstance(item, dict)]
        return parse_subtitle_text(candidate.read_text(encoding="utf-8", errors="ignore"))
    return []


def parse_subtitle_text(content: str) -> list[dict[str, Any]]:
    segments = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing = next((line for line in lines if "-->" in line), "")
        if not timing:
            continue
        left, right = [part.strip() for part in timing.split("-->", 1)]
        text = " ".join(line for line in lines if line != timing and not line.isdigit()).strip()
        if text:
            segments.append({"start": seconds_to_timecode(parse_timecode(left)), "end": seconds_to_timecode(parse_timecode(right)), "text": text})
    return segments


def clean_segment(item: dict[str, Any]) -> dict[str, Any]:
    start = item.get("start", "00:00:00.000")
    end = item.get("end", start)
    return {
        "start": seconds_to_timecode(parse_timecode(start)),
        "end": seconds_to_timecode(parse_timecode(end)),
        "text": str(item.get("text") or "").strip(),
    }


def fallback_segments(video_path: Path) -> list[dict[str, Any]]:
    info = get_video_info(video_path)
    duration = max(0.0, info.duration)
    if duration <= 0:
        return []
    segment_count = max(1, min(40, math.ceil(duration / 30)))
    length = duration / segment_count
    return [
        {
            "start": seconds_to_timecode(index * length),
            "end": seconds_to_timecode(min(duration, (index + 1) * length)),
            "text": f"Episode scene {index + 1}. Add sidecar subtitles or enable Whisper for dialogue-level AI analysis.",
        }
        for index in range(segment_count)
    ]


def write_subtitle_outputs(episode: int, segments: list[dict[str, Any]], subtitles_dir: Path) -> None:
    payload = {"episode": episode, "segments": segments}
    write_json(subtitles_dir / f"episode_{episode:02d}.json", payload)
    lines = []
    for index, segment in enumerate(segments, start=1):
        lines.extend(
            [
                str(index),
                f"{to_srt_time(segment['start'])} --> {to_srt_time(segment['end'])}",
                segment.get("text", ""),
                "",
            ]
        )
    (subtitles_dir / f"episode_{episode:02d}.srt").write_text("\n".join(lines), encoding="utf-8")


def subtitle_segments_for_mode(
    provider: AIProvider,
    segments: list[dict[str, Any]],
    episode: int,
    subtitles_dir: Path,
    options: DramaReelOptions,
    source: str,
) -> list[dict[str, Any]]:
    mode = (options.subtitle_mode or "english").lower()
    if source.startswith("placeholder"):
        return []
    if mode != "english":
        return segments
    if any(is_probably_english(str(item.get("text") or "")) for item in segments):
        return segments
    translated = translate_subtitle_segments(provider, segments)
    if translated:
        write_subtitle_outputs_named(episode, translated, subtitles_dir, "en")
        return translated
    return []


def translate_subtitle_segments(provider: AIProvider, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    real_segments = [item for item in segments if str(item.get("text") or "").strip() and not str(item.get("text")).startswith("Episode scene ")]
    if not real_segments or not provider.available():
        return []
    results: list[dict[str, Any]] = []
    chunk_size = 40
    for offset in range(0, len(real_segments), chunk_size):
        chunk = real_segments[offset : offset + chunk_size]
        try:
            payload = provider.analyze_json(
                system_prompt=(
                    "You translate short drama subtitles for Facebook Reels. "
                    "Return strict JSON only. Keep the same id for every segment. "
                    "Translate meaning naturally into concise spoken English. "
                    "Do not summarize, do not add marketing hooks."
                ),
                user_payload={
                    "segments": [
                        {"id": offset + index, "text": str(item.get("text") or "")}
                        for index, item in enumerate(chunk)
                    ],
                    "output_schema": {"segments": [{"id": 0, "text": "English translation"}]},
                },
            )
            translated_items = payload.get("segments") if isinstance(payload, dict) else None
            by_id = {
                int(item.get("id")): str(item.get("text") or "").strip()
                for item in translated_items or []
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            }
        except Exception:
            by_id = {}
        for index, item in enumerate(chunk):
            translated_text = by_id.get(offset + index, "")
            if translated_text:
                results.append({**item, "text": translated_text})
    return results


def write_subtitle_outputs_named(episode: int, segments: list[dict[str, Any]], subtitles_dir: Path, name: str) -> None:
    payload = {"episode": episode, "segments": segments}
    stem = f"episode_{episode:02d}.{name}"
    write_json(subtitles_dir / f"{stem}.json", payload)
    lines = []
    for index, item in enumerate(segments, start=1):
        lines.extend(
            [
                str(index),
                f"{to_srt_time(item['start'])} --> {to_srt_time(item['end'])}",
                str(item.get("text") or ""),
                "",
            ]
        )
    (subtitles_dir / f"{stem}.srt").write_text("\n".join(lines), encoding="utf-8")


def analyze_episode_highlights(
    *,
    provider: AIProvider,
    episode: int,
    video_path: Path,
    segments: list[dict[str, Any]],
    duration: float,
    options: DramaReelOptions,
) -> list[dict[str, Any]]:
    user_payload = {
        "episode": episode,
        "video_filename": video_path.name,
        "subtitle_segments": segments[:500],
        "target_seconds": options.target_seconds,
        "min_seconds": options.min_seconds,
        "max_seconds": options.max_seconds,
    }
    if provider.available():
        try:
            payload = provider.analyze_json(system_prompt=DRAMA_HIGHLIGHT_PROMPT, user_payload=user_payload)
            highlights = payload.get("highlights") if isinstance(payload, dict) else None
            if isinstance(highlights, list) and highlights:
                return normalize_highlights(highlights, duration, options)
        except Exception:
            pass
    return heuristic_highlights(segments, duration, options)


def heuristic_highlights(segments: list[dict[str, Any]], duration: float, options: DramaReelOptions) -> list[dict[str, Any]]:
    candidates = []
    for index, segment in enumerate(segments):
        text = segment.get("text", "")
        types = detect_types(text)
        if not types and "Episode scene" in text:
            types = ["悬念结尾"]
        if not types:
            continue
        start_seconds = parse_timecode(segment["start"])
        nearby = " ".join(item.get("text", "") for item in segments[max(0, index - 2) : index + 5])
        scores = score_candidate(types, nearby)
        clip_start = max(0.0, start_seconds - options.target_seconds * 0.25)
        clip_end = min(duration, clip_start + options.target_seconds)
        if clip_end - clip_start < options.min_seconds:
            clip_end = min(duration, clip_start + options.min_seconds)
        candidates.append(
            {
                "start": seconds_to_timecode(clip_start),
                "end": seconds_to_timecode(clip_end),
                "duration": round(max(0.0, clip_end - clip_start), 3),
                "type": types,
                **scores,
                "reason": "命中高冲突/反转关键词，适合作为 Facebook Reel 候选爆点。",
                "hook_text": hook_for_types(types),
                "caption": caption_for_types(types),
                "hashtags": DEFAULT_HASHTAGS,
                "cut_strategy": "保留冲突开头，结尾提前停在反应完成前制造悬念。",
            }
        )
    if not candidates and duration > 0:
        for start in sample_starts(duration, options):
            end = min(duration, start + options.target_seconds)
            types = ["悬念结尾"]
            candidates.append(
                {
                    "start": seconds_to_timecode(start),
                    "end": seconds_to_timecode(end),
                    "duration": round(end - start, 3),
                    "type": types,
                    **score_candidate(types, ""),
                    "reason": "没有可用字幕或关键词，按时长均匀采样生成待复核候选。",
                    "hook_text": hook_for_types(types),
                    "caption": "Watch until the truth comes out.",
                    "hashtags": DEFAULT_HASHTAGS,
                    "cut_strategy": "先生成候选片段，建议人工预览后确认。",
                }
            )
    return normalize_highlights(candidates, duration, options)


def normalize_highlights(raw: list[dict[str, Any]], duration: float, options: DramaReelOptions) -> list[dict[str, Any]]:
    items = []
    for item in raw:
        start = max(0.0, parse_timecode(item.get("start", 0)))
        end = min(duration, parse_timecode(item.get("end", start + options.target_seconds)))
        if end <= start:
            end = min(duration, start + options.target_seconds)
        if end - start > options.max_seconds:
            end = start + options.max_seconds
        if end - start < 3:
            continue
        types = item.get("type") or item.get("types") or ["悬念结尾"]
        if not isinstance(types, list):
            types = [str(types)]
        scores = {name: clamp_score(item.get(name, 65)) for name in score_keys()}
        if not item.get("overall_score"):
            scores["overall_score"] = weighted_score(scores)
        items.append(
            {
                "start": seconds_to_timecode(start),
                "end": seconds_to_timecode(end),
                "duration": round(end - start, 3),
                "type": [str(value) for value in types][:5],
                **scores,
                "reason": str(item.get("reason") or "适合短剧 Reel 的高冲突候选片段。"),
                "hook_text": str(item.get("hook_text") or hook_for_types(types)),
                "caption": str(item.get("caption") or caption_for_types(types)),
                "hashtags": item.get("hashtags") if isinstance(item.get("hashtags"), list) else DEFAULT_HASHTAGS,
                "cut_strategy": str(item.get("cut_strategy") or "保留开头冲突，结尾停在悬念处。"),
            }
        )
    items.sort(key=lambda value: float(value.get("overall_score") or 0), reverse=True)
    return items[: max(1, min(options.max_reels, 50))]


def suggest_combined_reels(provider: AIProvider, highlights: list[dict[str, Any]], options: DramaReelOptions) -> list[dict[str, Any]]:
    if len(highlights) < 2:
        return []
    if provider.available():
        try:
            payload = provider.analyze_json(
                system_prompt=COMBINED_REEL_PROMPT,
                user_payload={"highlights": highlights[:30], "target_seconds": options.target_seconds},
            )
            reels = payload.get("combined_reels")
            if isinstance(reels, list):
                return normalize_combined(reels)[:10]
        except Exception:
            pass
    by_episode: dict[int, list[dict[str, Any]]] = {}
    for item in highlights:
        by_episode.setdefault(int(item.get("episode") or 1), []).append(item)
    if len(by_episode) < 2:
        return []
    selected = [items[0] for _, items in sorted(by_episode.items())[:3] if items]
    return [
        {
            "id": "combined_reel_001",
            "clips": [
                {"episode": int(item["episode"]), "start": item["start"], "end": trim_end(item["start"], item["end"], 12)}
                for item in selected
            ],
            "reason": "把多集里的羞辱、冲突升级和反转组合成连续爽点。",
            "overall_score": round(sum(float(item.get("overall_score") or 0) for item in selected) / len(selected), 1),
        }
    ]


def normalize_combined(reels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for index, item in enumerate(reels, start=1):
        clips = item.get("clips")
        if not isinstance(clips, list) or not clips:
            continue
        results.append(
            {
                "id": str(item.get("id") or f"combined_reel_{index:03d}"),
                "clips": [
                    {
                        "episode": int(clip.get("episode") or 1),
                        "start": seconds_to_timecode(parse_timecode(clip.get("start", 0))),
                        "end": seconds_to_timecode(parse_timecode(clip.get("end", 0))),
                    }
                    for clip in clips
                    if isinstance(clip, dict)
                ],
                "reason": str(item.get("reason") or "组合多个爆点形成连续爽点。"),
                "overall_score": clamp_score(item.get("overall_score", 80)),
            }
        )
    return results


def generate_reels_from_plan(
    *,
    plan_path: Union[str, Path],
    selected_ids: list[str],
    output_dir: Union[str, Path],
    options: DramaReelOptions,
    task_id: str,
) -> list[Path]:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    episodes = {int(item["episode"]): Path(item["path"]) for item in plan.get("episodes", [])}
    selected = set(selected_ids or [])
    outputs: list[Path] = []
    for reel in plan.get("single_reels", []):
        if selected and reel["id"] not in selected:
            continue
        source = Path(reel.get("source_file") or episodes.get(int(reel.get("episode") or 1), ""))
        if not source.exists():
            continue
        target = output_path / f"{safe_stem(reel['id'])}.mp4"
        render_single_reel(source, target, reel, options, task_id)
        outputs.append(target)
    for reel in plan.get("combined_reels", []):
        if selected and reel["id"] not in selected:
            continue
        target = output_path / f"{safe_stem(reel['id'])}.mp4"
        render_combined_reel(episodes, target, reel, options, task_id)
        outputs.append(target)
    return outputs


def reel_subtitle_segments(
    segments: list[dict[str, Any]],
    reel: dict[str, Any],
    options: DramaReelOptions,
) -> list[dict[str, Any]]:
    clip_start = parse_timecode(reel.get("start"))
    clip_end = parse_timecode(reel.get("end"))
    duration = max(0.1, clip_end - clip_start)
    clipped: list[dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text or text.startswith("Episode scene "):
            continue
        start = parse_timecode(segment.get("start"))
        end = parse_timecode(segment.get("end"))
        if end <= clip_start or start >= clip_end:
            continue
        clipped.append(
            {
                "start": seconds_to_timecode(max(0.0, start - clip_start)),
                "end": seconds_to_timecode(min(duration, end - clip_start)),
                "text": text,
            }
        )

    mode = (options.subtitle_mode or "english").lower()
    if mode == "english":
        english = [item for item in clipped if is_probably_english(str(item.get("text") or ""))]
        return english[:24]
    if mode == "bilingual" and clipped:
        return clipped[:24]
    return clipped[:24]


def marketing_subtitle_segments(reel: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    hook = str(reel.get("hook_text") or "").strip()
    caption = str(reel.get("caption") or "").strip()
    reason = str(reel.get("reason") or "").strip()
    texts = [value for value in [hook, caption, english_reason_line(reason)] if value]
    if not texts:
        texts = ["Watch until the truth comes out."]
    cue_count = min(len(texts), 3)
    span = max(2.6, duration / max(cue_count, 1))
    cues = []
    for index, text in enumerate(texts[:cue_count]):
        start = min(duration - 0.1, index * span)
        end = min(duration, start + max(2.8, span))
        cues.append({"start": seconds_to_timecode(start), "end": seconds_to_timecode(end), "text": text})
    return cues


def english_reason_line(reason: str) -> str:
    if not reason or not is_probably_english(reason):
        return ""
    return reason[:90]


def is_probably_english(text: str) -> bool:
    cleaned = re.sub(r"[\W\d_]+", "", text, flags=re.ASCII)
    ascii_letters = sum(1 for char in text if ("a" <= char.lower() <= "z"))
    non_ascii = sum(1 for char in text if ord(char) > 127)
    return ascii_letters >= 8 and ascii_letters >= non_ascii * 2 and bool(cleaned)


def render_single_reel(source: Path, target: Path, reel: dict[str, Any], options: DramaReelOptions, task_id: str) -> None:
    subtitle_paths: list[Path] = []
    if options.burn_subtitles:
        subtitle_paths = create_subtitle_overlays(reel, target)
    filter_args, video_map = reel_filter_args(options.frame_mode, subtitle_paths)
    command = [
        ffmpeg_bin(),
        "-y",
        "-ss",
        f"{parse_timecode(reel['start']):.3f}",
        "-i",
        str(source),
        "-t",
        f"{parse_timecode(reel['end']) - parse_timecode(reel['start']):.3f}",
    ]
    for subtitle_path in subtitle_paths:
        command += ["-loop", "1", "-i", str(subtitle_path)]
    command += [
        *filter_args,
        "-map",
        video_map,
        "-map",
        "0:a?",
        "-shortest",
        *_mp4_compat_args(),
        str(target),
    ]
    try:
        run_ffmpeg(command, task_id)
    finally:
        for subtitle_path in subtitle_paths:
            subtitle_path.unlink(missing_ok=True)


def render_combined_reel(episodes: dict[int, Path], target: Path, reel: dict[str, Any], options: DramaReelOptions, task_id: str) -> None:
    work_dir = target.parent / f".{target.stem}_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for index, clip in enumerate(reel.get("clips", []), start=1):
        source = episodes.get(int(clip.get("episode") or 1))
        if not source or not source.exists():
            continue
        part = work_dir / f"part_{index:03d}.mp4"
        render_single_reel(source, part, clip, options, task_id)
        parts.append(part)
    if not parts:
        raise RuntimeError(f"组合 Reel 没有可用片段：{reel.get('id')}")
    concat_file = work_dir / "concat.txt"
    concat_file.write_text("".join(f"file '{part.as_posix()}'\n" for part in parts), encoding="utf-8")
    run_ffmpeg([ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(target)], task_id)
    shutil.rmtree(work_dir, ignore_errors=True)


def reel_filter_args(frame_mode: str, subtitle_paths: list[Path] | None = None) -> tuple[list[str], str]:
    subtitle_paths = subtitle_paths or []
    subtitle_graph = ""
    video_label = "[v0]"
    if frame_mode == "crop":
        graph = (
            f"[0:v]scale={EXPORT_WIDTH}:{EXPORT_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={EXPORT_WIDTH}:{EXPORT_HEIGHT},fps={EXPORT_FPS},setsar=1[v0]"
        )
    else:
        graph = (
            f"[0:v]scale={EXPORT_WIDTH}:{EXPORT_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={EXPORT_WIDTH}:{EXPORT_HEIGHT},boxblur=24:12[bg];"
            f"[0:v]scale={EXPORT_WIDTH}:{EXPORT_HEIGHT}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={EXPORT_FPS},setsar=1[v0]"
        )
    previous = "[v0]"
    for index, subtitle_path in enumerate(subtitle_paths, start=1):
        cue = parse_subtitle_overlay_name(subtitle_path)
        output_label = "[v]" if index == len(subtitle_paths) else f"[vsub{index}]"
        subtitle_graph += (
            f";[{index}:v]format=rgba[sub{index}];"
            f"{previous}[sub{index}]overlay=0:0:enable='between(t,{cue['start']:.3f},{cue['end']:.3f})'"
            f"{output_label}"
        )
        previous = output_label
    if subtitle_graph:
        graph = f"{graph}{subtitle_graph}"
        video_label = previous
    return ["-filter_complex", graph], video_label


def subtitle_text(reel: dict[str, Any]) -> str:
    return str(reel.get("hook_text") or reel.get("caption") or "").strip()


def create_subtitle_overlays(reel: dict[str, Any], target: Path) -> list[Path]:
    cues = subtitle_cues_for_render(reel)
    paths: list[Path] = []
    for index, cue in enumerate(cues[:24], start=1):
        output_path = target.with_suffix(f".subtitle_{index:03d}_{cue['start']:.3f}_{cue['end']:.3f}.png")
        create_subtitle_overlay(str(cue["text"]), output_path)
        paths.append(output_path)
    return paths


def subtitle_cues_for_render(reel: dict[str, Any]) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    duration = max(0.1, parse_timecode(reel.get("end")) - parse_timecode(reel.get("start")))
    for segment in reel.get("subtitle_segments") or []:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, parse_timecode(segment.get("start", 0)))
        end = min(duration, parse_timecode(segment.get("end", start + 2.8)))
        if end <= start:
            end = min(duration, start + 2.8)
        cues.append({"start": start, "end": end, "text": text})
    if cues:
        return cues
    fallback = subtitle_text(reel)
    if not fallback:
        return []
    midpoint = min(duration, max(2.8, duration * 0.45))
    return [
        {"start": 0.0, "end": midpoint, "text": fallback},
        {"start": max(0.0, duration - 4.0), "end": duration, "text": str(reel.get("caption") or fallback)},
    ]


def parse_subtitle_overlay_name(path: Path) -> dict[str, float]:
    match = re.search(r"\.subtitle_\d+_(\d+\.\d+)_(\d+\.\d+)\.png$", path.name)
    if not match:
        return {"start": 0.0, "end": 9999.0}
    return {"start": float(match.group(1)), "end": float(match.group(2))}


def create_subtitle_overlay(text: str, output_path: Path) -> Path:
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", text.strip())[:140]
    image = Image.new("RGBA", (EXPORT_WIDTH, EXPORT_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    band_top = int(EXPORT_HEIGHT * 0.66)
    draw.rounded_rectangle(
        (34, band_top, EXPORT_WIDTH - 34, int(EXPORT_HEIGHT * 0.86)),
        radius=22,
        fill=(0, 0, 0, 136),
    )
    font = load_subtitle_font(42)
    lines = wrap_text(cleaned, font, EXPORT_WIDTH - 110, draw)
    line_height = 54
    total_height = len(lines) * line_height
    y = band_top + max(20, (int(EXPORT_HEIGHT * 0.20) - total_height) // 2)
    for line in lines[:3]:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=4)
        x = (EXPORT_WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0, 255))
        y += line_height
    image.save(output_path)
    return output_path


def load_subtitle_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    text = text.replace("\n", " / ")
    if " " not in text and any(ord(char) > 127 for char in text):
        words = list(text)
    else:
        words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        separator = "" if len(word) == 1 and any(ord(char) > 127 for char in word) else " "
        probe = f"{current}{separator}{word}".strip()
        bbox = draw.textbbox((0, 0), probe, font=font, stroke_width=4)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = probe
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:3]


def drawtext_supported() -> bool:
    global _DRAWTEXT_SUPPORTED
    if _DRAWTEXT_SUPPORTED is not None:
        return _DRAWTEXT_SUPPORTED
    try:
        result = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        _DRAWTEXT_SUPPORTED = " drawtext " in result.stdout
    except Exception:
        _DRAWTEXT_SUPPORTED = False
    return _DRAWTEXT_SUPPORTED


def run_ffmpeg(command: list[str], task_id: str) -> subprocess.CompletedProcess[str]:
    result = run_cancellable(command, task_id=task_id)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"FFmpeg failed, code={result.returncode}: {stderr[-1600:]}")
    return result


def package_outputs(zip_path: Path, paths: list[Path]) -> None:
    import zipfile

    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        archive.write(child, child.relative_to(path.parent))
            elif path.exists():
                archive.write(path, path.name)


def detect_types(text: str) -> list[str]:
    lowered = text.lower()
    hits = [tag for tag, keywords in TYPE_KEYWORDS.items() if any(keyword.lower() in lowered for keyword in keywords)]
    return hits[:5]


def score_candidate(types: list[str], text: str) -> dict[str, int]:
    type_set = set(types)
    hook = 72 + (8 if type_set & {"打脸", "冲突升级", "身份反转"} else 0)
    conflict = 70 + (12 if type_set & {"冲突升级", "威胁", "离婚", "背叛"} else 0)
    reverse = 62 + (22 if type_set & {"身份反转", "霸总/富豪身份曝光", "反转"} else 0)
    emotion = 66 + (16 if type_set & {"情绪爆发", "下跪", "背叛"} else 0)
    suspense = 68 + (14 if type_set & {"悬念结尾", "误会", "反转"} else 0)
    comment = 65 + (15 if type_set & {"评论诱发点", "离婚", "误会"} else 0)
    completion = 72 + min(10, len(text) // 90)
    scores = {
        "hook_score": clamp_score(hook),
        "conflict_score": clamp_score(conflict),
        "reverse_score": clamp_score(reverse),
        "emotion_score": clamp_score(emotion),
        "suspense_score": clamp_score(suspense),
        "comment_score": clamp_score(comment),
        "completion_score": clamp_score(completion),
    }
    scores["overall_score"] = weighted_score(scores)
    return scores


def weighted_score(scores: dict[str, Any]) -> int:
    value = (
        float(scores.get("hook_score", 0)) * 0.2
        + float(scores.get("conflict_score", 0)) * 0.2
        + float(scores.get("reverse_score", 0)) * 0.2
        + float(scores.get("emotion_score", 0)) * 0.15
        + float(scores.get("suspense_score", 0)) * 0.1
        + float(scores.get("comment_score", 0)) * 0.1
        + float(scores.get("completion_score", 0)) * 0.05
    )
    return clamp_score(value)


def score_keys() -> list[str]:
    return [
        "hook_score",
        "conflict_score",
        "reverse_score",
        "emotion_score",
        "suspense_score",
        "comment_score",
        "completion_score",
        "overall_score",
    ]


def clamp_score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def hook_for_types(types: Iterable[str]) -> str:
    joined = " ".join(types)
    if "身份" in joined or "富豪" in joined:
        return "Everyone laughed at him... until they found out who he really was."
    if "背叛" in joined or "离婚" in joined:
        return "She trusted him, then one sentence destroyed everything."
    if "复仇" in joined:
        return "They thought she was weak. Her revenge starts now."
    return "The moment everyone underestimated them changed everything."


def caption_for_types(types: Iterable[str]) -> str:
    joined = " ".join(types)
    if "打脸" in joined or "身份" in joined:
        return "They messed with the wrong person."
    if "背叛" in joined:
        return "Betrayal always has a price."
    return "Watch what happens next."


def sample_starts(duration: float, options: DramaReelOptions) -> list[float]:
    count = max(1, min(options.max_reels, math.ceil(duration / max(options.target_seconds, 1))))
    spacing = max(options.target_seconds, duration / count)
    return [round(min(duration - 3, index * spacing), 3) for index in range(count)]


def parse_timecode(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "0").strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(text)
    except ValueError:
        return 0.0


def seconds_to_timecode(value: float) -> str:
    seconds = max(0.0, float(value))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    rest = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{rest:06.3f}"


def to_srt_time(value: Any) -> str:
    return seconds_to_timecode(parse_timecode(value)).replace(".", ",")


def trim_end(start: str, end: str, seconds: float) -> str:
    start_seconds = parse_timecode(start)
    end_seconds = parse_timecode(end)
    return seconds_to_timecode(min(end_seconds, start_seconds + seconds))


def single_clip_command_preview(source: Path, reel: dict[str, Any], output_dir: Path) -> str:
    target = output_dir / f"{safe_stem(str(reel.get('id') or 'reel'))}.mp4"
    return f'ffmpeg -i "{source}" -ss {reel["start"]} -to {reel["end"]} -c copy "{target}"'


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
