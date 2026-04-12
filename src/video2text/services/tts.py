"""TTS 服务抽象层 + CosyVoice 实现。

支持功能：
- 预置音色 TTS
- 声音克隆（zero-shot）
- 字级时间戳（用于音视频对齐）

扩展点：FishSpeech 等其他 TTS 引擎可继承 TTSProvider 实现。
"""

from __future__ import annotations

import json
import logging
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from video2text.config.settings import Settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class WordTimestamp:
    """单个词/字的时间戳。"""
    word: str
    begin_ms: int
    end_ms: int


@dataclass
class TTSResult:
    """TTS 合成结果。"""
    audio_data: bytes
    audio_format: str = "wav"
    sample_rate: int = 22050
    duration_ms: int = 0
    word_timestamps: list[WordTimestamp] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class TTSProvider(ABC):
    """TTS 引擎抽象接口。"""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        model: str = "",
        speed: float = 1.0,
        enable_word_timestamps: bool = False,
    ) -> TTSResult:
        ...

    @abstractmethod
    def clone_voice(
        self,
        reference_audio: bytes | Path,
        text: str,
        *,
        model: str = "",
        speed: float = 1.0,
        enable_word_timestamps: bool = False,
    ) -> TTSResult:
        ...


# ---------------------------------------------------------------------------
# CosyVoice 实现（DashScope WebSocket API）
# ---------------------------------------------------------------------------

_COSYVOICE_WSS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


class CosyVoiceTTS(TTSProvider):
    """阿里云 CosyVoice TTS — 通过 DashScope WebSocket API 调用。

    支持：
    - 预置音色合成
    - 声音克隆（通过 voice enrollment API 或 zero-shot URL 参考）
    - 字级时间戳（word_timestamp_enabled）
    """

    def __init__(self, api_key: str, model: str = "cosyvoice-v3-flash"):
        self.api_key = api_key
        self.model = model

    def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        model: str = "",
        speed: float = 1.0,
        enable_word_timestamps: bool = False,
    ) -> TTSResult:
        effective_model = model or self.model
        return self._run_ws_tts(
            text=text,
            voice=voice_id,
            model=effective_model,
            speed=speed,
            enable_timestamps=enable_word_timestamps,
        )

    def clone_voice(
        self,
        reference_audio: bytes | Path,
        text: str,
        *,
        model: str = "",
        speed: float = 1.0,
        enable_word_timestamps: bool = False,
    ) -> TTSResult:
        effective_model = model or self.model

        if isinstance(reference_audio, Path):
            audio_data = reference_audio.read_bytes()
        else:
            audio_data = reference_audio

        return self._run_ws_clone(
            text=text,
            reference_audio=audio_data,
            model=effective_model,
            speed=speed,
            enable_timestamps=enable_word_timestamps,
        )

    # ----- 内部实现 -----

    _TTS_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"

    def _run_ws_tts(
        self,
        text: str,
        voice: str,
        model: str,
        speed: float,
        enable_timestamps: bool,
    ) -> TTSResult:
        """通过 DashScope 非流式 SpeechSynthesizer API 调用 CosyVoice。"""
        import urllib.request
        import urllib.error

        input_obj: dict[str, Any] = {
            "text": text,
            "voice": voice,
            "format": "wav",
            "sample_rate": 22050,
        }
        if speed != 1.0:
            input_obj["rate"] = speed
        if enable_timestamps:
            input_obj["word_timestamp_enabled"] = True

        body: dict[str, Any] = {
            "model": model,
            "input": input_obj,
        }

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(self._TTS_URL, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                rsp = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"CosyVoice TTS 失败 HTTP {e.code}: {err_body}") from e

        output = rsp.get("output") or {}
        audio_info = output.get("audio") or {}
        audio_url = audio_info.get("url", "")

        if not audio_url:
            raise RuntimeError(f"CosyVoice TTS 无音频 URL: {rsp}")

        return self._download_tts_audio(audio_url, enable_timestamps, output)

    def _run_ws_clone(
        self,
        text: str,
        reference_audio: bytes,
        model: str,
        speed: float,
        enable_timestamps: bool,
    ) -> TTSResult:
        """声音克隆：先上传参考音频到 OSS，再调用 TTS。"""
        from dashscope.utils.oss_utils import check_and_upload_local
        import tempfile
        import urllib.request
        import urllib.error

        suffix = ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(reference_audio)
            tmp_path = f.name

        try:
            _, audio_url, _ = check_and_upload_local(
                model, tmp_path, self.api_key, None
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        input_obj: dict[str, Any] = {
            "text": text,
            "voice": audio_url,
            "format": "wav",
            "sample_rate": 22050,
        }
        if speed != 1.0:
            input_obj["rate"] = speed
        if enable_timestamps:
            input_obj["word_timestamp_enabled"] = True

        body: dict[str, Any] = {
            "model": model,
            "input": input_obj,
        }

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-OssResourceResolve": "enable",
        }
        req = urllib.request.Request(self._TTS_URL, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                rsp = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"CosyVoice 克隆 TTS 失败 HTTP {e.code}: {err_body}") from e

        output = rsp.get("output") or {}
        audio_info = output.get("audio") or {}
        audio_url_result = audio_info.get("url", "")

        if not audio_url_result:
            raise RuntimeError(f"CosyVoice 克隆 TTS 无音频 URL: {rsp}")

        return self._download_tts_audio(audio_url_result, enable_timestamps, output)

    def _download_tts_audio(
        self,
        audio_url: str,
        with_timestamps: bool,
        output: dict[str, Any],
    ) -> TTSResult:
        """下载 TTS 音频并解析时间戳。"""
        import urllib.request

        audio_req = urllib.request.Request(audio_url)
        with urllib.request.urlopen(audio_req, timeout=120) as aresp:
            audio_data = aresp.read()

        timestamps: list[WordTimestamp] = []
        if with_timestamps:
            ts_data = output.get("word_timestamps") or output.get("timestamps") or []
            for item in ts_data:
                timestamps.append(WordTimestamp(
                    word=item.get("word", ""),
                    begin_ms=int(item.get("begin_time", 0)),
                    end_ms=int(item.get("end_time", 0)),
                ))

        duration_ms = 0
        if timestamps:
            duration_ms = max(t.end_ms for t in timestamps)
        elif len(audio_data) > 44:
            duration_ms = _estimate_wav_duration_ms(audio_data)

        return TTSResult(
            audio_data=audio_data,
            audio_format="wav",
            sample_rate=22050,
            duration_ms=duration_ms,
            word_timestamps=timestamps,
        )


def _estimate_wav_duration_ms(wav_data: bytes) -> int:
    """从 WAV 文件头估算时长（毫秒）。"""
    if len(wav_data) < 44:
        return 0
    try:
        data_size = len(wav_data) - 44
        sample_rate = struct.unpack_from("<I", wav_data, 24)[0]
        bits_per_sample = struct.unpack_from("<H", wav_data, 34)[0]
        channels = struct.unpack_from("<H", wav_data, 22)[0]
        if sample_rate == 0 or bits_per_sample == 0 or channels == 0:
            return 0
        bytes_per_sample = bits_per_sample // 8
        total_samples = data_size // (bytes_per_sample * channels)
        return int(total_samples * 1000 / sample_rate)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def get_tts_provider(settings: Settings) -> TTSProvider:
    """根据配置创建 TTS 引擎实例。"""
    provider = settings.tts_provider
    if provider == "cosyvoice":
        return CosyVoiceTTS(
            api_key=settings.dashscope_api_key,
            model=settings.tts_model,
        )
    raise ValueError(f"不支持的 TTS 引擎: {provider}（当前仅支持 cosyvoice）")
