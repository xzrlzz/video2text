"""逐镜头 TTS 音频与视频时长对齐。

核心逻辑：
1. 解析每个 Shot 的对白 → 确定说话角色 → 查找角色音色
2. 调用 TTS 合成每条对白
3. 根据 Shot.duration 调整 TTS 音频时长（速率调整/静音填充/裁剪）
4. 合并多个 Shot 的音频为完整 chunk 音频轨
"""

from __future__ import annotations

import logging
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from video2text.config.settings import Settings
from video2text.core.dialogue_parser import DialogueLine, parse_dialogue
from video2text.core.storyboard import Shot
from video2text.services.tts import TTSProvider, TTSResult, get_tts_provider

log = logging.getLogger(__name__)

if __import__("typing").TYPE_CHECKING:
    from video2text.core.ip_manager import IPCharacter, IPProfile


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class ShotAudioPlan:
    """单个镜头的音频计划。"""
    shot_index: int
    duration_ms: int                # 目标视频时长（毫秒）
    dialogue_lines: list[DialogueLine] = field(default_factory=list)
    voice_id: str = ""              # 该镜头使用的音色 ID
    is_narrator: bool = False       # 是否全部为旁白
    tts_result: TTSResult | None = None
    adjusted_audio: bytes | None = None


@dataclass
class ChunkAudioResult:
    """一个 chunk 的完整音频结果。"""
    audio_data: bytes
    audio_format: str = "wav"
    sample_rate: int = 22050
    duration_ms: int = 0
    shot_plans: list[ShotAudioPlan] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 角色 → 音色映射
# ---------------------------------------------------------------------------


def resolve_voice_for_speaker(
    speaker: str,
    characters: list[IPCharacter],
    narrator_voice_id: str = "",
) -> str:
    """根据说话角色名找到对应的音色 ID。"""
    speaker_lower = speaker.strip().lower()

    for char in characters:
        names = [char.name.lower(), char.name_en.lower()]
        names = [n for n in names if n]
        if any(speaker_lower == n or speaker_lower in n or n in speaker_lower for n in names):
            vid = char.voice_profile.effective_voice_id
            if vid:
                return vid

    if narrator_voice_id:
        return narrator_voice_id

    return ""


# ---------------------------------------------------------------------------
# 音频时长调整
# ---------------------------------------------------------------------------


def adjust_audio_duration(
    audio_data: bytes,
    target_ms: int,
    sample_rate: int = 22050,
) -> bytes:
    """将 WAV 音频调整到目标时长。

    策略：
    - 音频比目标短 → 末尾填充静音
    - 音频比目标长 → 用 ffmpeg atempo 加速（不超过 2.0x）
    - 时长差异 <5% → 不调整
    """
    if len(audio_data) < 44:
        return _generate_silence_wav(target_ms, sample_rate)

    current_ms = _wav_duration_ms(audio_data, sample_rate)
    if current_ms <= 0:
        return _generate_silence_wav(target_ms, sample_rate)

    ratio = current_ms / target_ms if target_ms > 0 else 1.0

    if 0.95 <= ratio <= 1.05:
        return audio_data

    if ratio < 1.0:
        padding_ms = target_ms - current_ms
        silence = _generate_silence_pcm(padding_ms, sample_rate)
        return _append_pcm_to_wav(audio_data, silence, sample_rate)

    speed = min(2.0, ratio)
    return _ffmpeg_atempo(audio_data, speed)


def _wav_duration_ms(wav_data: bytes, default_sr: int = 22050) -> int:
    if len(wav_data) < 44:
        return 0
    try:
        sr = struct.unpack_from("<I", wav_data, 24)[0] or default_sr
        bps = struct.unpack_from("<H", wav_data, 34)[0] or 16
        ch = struct.unpack_from("<H", wav_data, 22)[0] or 1
        data_size = len(wav_data) - 44
        samples = data_size // (bps // 8 * ch)
        return int(samples * 1000 / sr)
    except Exception:
        return 0


def _generate_silence_wav(duration_ms: int, sample_rate: int = 22050) -> bytes:
    """生成指定时长的静音 WAV。"""
    pcm = _generate_silence_pcm(duration_ms, sample_rate)
    return _pcm_to_wav(pcm, sample_rate)


def _generate_silence_pcm(duration_ms: int, sample_rate: int = 22050) -> bytes:
    num_samples = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * num_samples


def _pcm_to_wav(pcm: bytes, sample_rate: int = 22050, channels: int = 1, bits: int = 16) -> bytes:
    """将 PCM 数据包装为 WAV 格式。"""
    data_size = len(pcm)
    file_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", file_size, b"WAVE",
        b"fmt ", 16, 1, channels,
        sample_rate, sample_rate * channels * bits // 8,
        channels * bits // 8, bits,
        b"data", data_size,
    )
    return header + pcm


def _append_pcm_to_wav(wav_data: bytes, extra_pcm: bytes, sample_rate: int) -> bytes:
    """在 WAV 音频末尾追加 PCM 数据（静音填充）。"""
    if len(wav_data) < 44:
        return wav_data
    pcm = wav_data[44:] + extra_pcm
    return _pcm_to_wav(pcm, sample_rate)


def _ffmpeg_atempo(wav_data: bytes, speed: float) -> bytes:
    """使用 ffmpeg atempo 滤镜调整播放速度。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_in:
        f_in.write(wav_data)
        in_path = f_in.name
    out_path = in_path + ".out.wav"
    try:
        filters = []
        s = speed
        while s > 2.0:
            filters.append("atempo=2.0")
            s /= 2.0
        filters.append(f"atempo={s:.4f}")
        af = ",".join(filters)

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", in_path,
            "-af", af,
            out_path,
        ]
        subprocess.run(cmd, check=True, timeout=30)
        return Path(out_path).read_bytes()
    except Exception:
        log.warning("ffmpeg atempo 调整失败，返回原始音频")
        return wav_data
    finally:
        Path(in_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 多镜头音频合成
# ---------------------------------------------------------------------------


def build_chunk_audio(
    shots: list[Shot],
    ip_profile: IPProfile,
    settings: Settings,
    *,
    progress_cb: Callable[[str], None] | None = None,
) -> ChunkAudioResult:
    """为一组镜头（chunk）生成完整的对齐音频轨。"""
    cb = progress_cb or (lambda _: None)
    tts = get_tts_provider(settings)
    characters = ip_profile.characters
    narrator_vid = ip_profile.narrator_voice.effective_voice_id

    plans: list[ShotAudioPlan] = []
    for i, shot in enumerate(shots):
        target_ms = int(float(shot.duration) * 1000)
        lines = parse_dialogue(shot.dialogue or "")

        voice_id = ""
        is_narrator = True
        if lines:
            for dl in lines:
                if not dl.is_narrator:
                    is_narrator = False
                    vid = resolve_voice_for_speaker(dl.speaker, characters, narrator_vid)
                    if vid:
                        voice_id = vid
                        break
            if not voice_id and narrator_vid:
                voice_id = narrator_vid

        plans.append(ShotAudioPlan(
            shot_index=i,
            duration_ms=target_ms,
            dialogue_lines=lines,
            voice_id=voice_id,
            is_narrator=is_narrator,
        ))

    for plan in plans:
        if not plan.dialogue_lines or not plan.voice_id:
            plan.adjusted_audio = _generate_silence_wav(plan.duration_ms)
            cb(f"镜头 {plan.shot_index + 1}: 无对白/无音色 → 静音 {plan.duration_ms}ms")
            continue

        full_text = " ".join(dl.clean_text for dl in plan.dialogue_lines)
        if not full_text.strip():
            plan.adjusted_audio = _generate_silence_wav(plan.duration_ms)
            continue

        try:
            result = tts.synthesize(
                text=full_text,
                voice_id=plan.voice_id,
                model=settings.tts_model,
                enable_word_timestamps=True,
            )
            plan.tts_result = result
            plan.adjusted_audio = adjust_audio_duration(
                result.audio_data, plan.duration_ms, result.sample_rate,
            )
            cb(
                f"镜头 {plan.shot_index + 1}: TTS 完成 "
                f"({result.duration_ms}ms → {plan.duration_ms}ms)"
            )
        except Exception as e:
            log.warning("镜头 %d TTS 失败: %s", plan.shot_index + 1, e)
            plan.adjusted_audio = _generate_silence_wav(plan.duration_ms)
            cb(f"镜头 {plan.shot_index + 1}: TTS 失败，填充静音")

    all_pcm = b""
    sr = 22050
    for plan in plans:
        if plan.adjusted_audio and len(plan.adjusted_audio) > 44:
            all_pcm += plan.adjusted_audio[44:]
        else:
            all_pcm += _generate_silence_pcm(plan.duration_ms, sr)

    combined_wav = _pcm_to_wav(all_pcm, sr)
    total_ms = sum(p.duration_ms for p in plans)

    return ChunkAudioResult(
        audio_data=combined_wav,
        sample_rate=sr,
        duration_ms=total_ms,
        shot_plans=plans,
    )
