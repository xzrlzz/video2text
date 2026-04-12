"""IP 资产管理：数据模型、CRUD 操作和文件存储。

目录结构:
  data/ip/<username>/<ip_id>/
    ip.json                   # 完整 IP 元数据
    characters/
      <char_id>/
        reference.jpg          # 角色参考图
        meta.json              # 角色元数据快照
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from video2text.utils.paths import get_data_dir


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class VisualDNA:
    style_preset_id: str = ""
    style_keywords: str = ""
    style_keywords_en: str = ""
    color_tone: str = ""
    lighting_preference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VisualDNA:
        return cls(**{k: str(d.get(k, "")) for k in cls.__dataclass_fields__})


@dataclass
class StoryDNA:
    genre: str = ""
    narrative_pattern: str = ""
    emotional_tone: str = ""
    pacing: str = ""
    episode_structure: str = ""
    typical_plot_hooks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoryDNA:
        return cls(
            genre=str(d.get("genre", "")),
            narrative_pattern=str(d.get("narrative_pattern", "")),
            emotional_tone=str(d.get("emotional_tone", "")),
            pacing=str(d.get("pacing", "")),
            episode_structure=str(d.get("episode_structure", "")),
            typical_plot_hooks=list(d.get("typical_plot_hooks") or []),
        )


@dataclass
class WorldDNA:
    primary_setting: str = ""
    recurring_locations: list[str] = field(default_factory=list)
    world_rules: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorldDNA:
        return cls(
            primary_setting=str(d.get("primary_setting", "")),
            recurring_locations=list(d.get("recurring_locations") or []),
            world_rules=str(d.get("world_rules", "")),
        )


@dataclass
class VoiceProfile:
    """角色音色配置。"""
    mode: str = ""                   # "preset" | "clone" | ""（未设置）
    preset_id: str = ""              # CosyVoice 预置音色 ID（如 "longshu_v3"）
    preset_name: str = ""            # 显示名（如 "沉稳青年男"）
    reference_audio_path: str = ""   # 克隆模式：用户上传的参考音频本地路径
    reference_audio_url: str = ""    # 上传到 OSS 后的公网 URL（wan2.7 用）
    clone_voice_id: str = ""         # CosyVoice 克隆后的 voice_id
    provider: str = "cosyvoice"      # "cosyvoice" | "fish_speech"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VoiceProfile:
        if not d:
            return cls()
        return cls(**{k: str(d.get(k, "")) for k in cls.__dataclass_fields__})

    @property
    def is_configured(self) -> bool:
        return bool(self.mode)

    @property
    def effective_voice_id(self) -> str:
        """返回 TTS 调用时使用的 voice ID。"""
        if self.mode == "clone" and self.clone_voice_id:
            return self.clone_voice_id
        if self.mode == "preset" and self.preset_id:
            return self.preset_id
        return ""

    @property
    def effective_audio_url(self) -> str:
        """返回 wan2.7 reference_voice 使用的音频 URL。"""
        return self.reference_audio_url or ""


@dataclass
class IPCharacter:
    id: str = ""
    name: str = ""
    name_en: str = ""
    role: str = "supporting"  # protagonist / supporting
    visual_description: str = ""
    personality: str = ""
    behavior_patterns: list[str] = field(default_factory=list)
    relationship: str = ""
    reference_image_path: str = ""
    reference_type: str = ""  # generated / uploaded
    voice_profile: VoiceProfile = field(default_factory=VoiceProfile)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["voice_profile"] = self.voice_profile.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IPCharacter:
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            name_en=str(d.get("name_en", "")),
            role=str(d.get("role", "supporting")),
            visual_description=str(d.get("visual_description", "")),
            personality=str(d.get("personality", "")),
            behavior_patterns=list(d.get("behavior_patterns") or []),
            relationship=str(d.get("relationship", "")),
            reference_image_path=str(d.get("reference_image_path", "")),
            reference_type=str(d.get("reference_type", "")),
            voice_profile=VoiceProfile.from_dict(d.get("voice_profile") or {}),
        )


@dataclass
class IPProfile:
    id: str = ""
    name: str = ""
    name_en: str = ""
    tagline: str = ""
    visual_dna: VisualDNA = field(default_factory=VisualDNA)
    story_dna: StoryDNA = field(default_factory=StoryDNA)
    world_dna: WorldDNA = field(default_factory=WorldDNA)
    characters: list[IPCharacter] = field(default_factory=list)
    narrator_voice: VoiceProfile = field(default_factory=VoiceProfile)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "name_en": self.name_en,
            "tagline": self.tagline,
            "visual_dna": self.visual_dna.to_dict(),
            "story_dna": self.story_dna.to_dict(),
            "world_dna": self.world_dna.to_dict(),
            "characters": [c.to_dict() for c in self.characters],
            "narrator_voice": self.narrator_voice.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IPProfile:
        return cls(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            name_en=str(d.get("name_en", "")),
            tagline=str(d.get("tagline", "")),
            visual_dna=VisualDNA.from_dict(d.get("visual_dna") or {}),
            story_dna=StoryDNA.from_dict(d.get("story_dna") or {}),
            world_dna=WorldDNA.from_dict(d.get("world_dna") or {}),
            characters=[
                IPCharacter.from_dict(c) for c in (d.get("characters") or [])
            ],
            narrator_voice=VoiceProfile.from_dict(d.get("narrator_voice") or {}),
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
        )

    def get_character(self, char_id: str) -> IPCharacter | None:
        for c in self.characters:
            if c.id == char_id:
                return c
        return None

    def get_protagonists(self) -> list[IPCharacter]:
        return [c for c in self.characters if c.role == "protagonist"]

    def get_all_character_names(self) -> list[str]:
        return [c.name for c in self.characters if c.name]


# ---------------------------------------------------------------------------
# 文件系统 CRUD
# ---------------------------------------------------------------------------


def _ip_base_dir(username: str) -> Path:
    return get_data_dir() / "ip" / username


def _ip_dir(username: str, ip_id: str) -> Path:
    return _ip_base_dir(username) / ip_id


def _ip_json_path(username: str, ip_id: str) -> Path:
    return _ip_dir(username, ip_id) / "ip.json"


def _char_dir(username: str, ip_id: str, char_id: str) -> Path:
    return _ip_dir(username, ip_id) / "characters" / char_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_ip(username: str, profile: IPProfile) -> Path:
    """保存 IP 元数据到 JSON 文件，返回文件路径。"""
    profile.updated_at = _now_iso()
    if not profile.created_at:
        profile.created_at = profile.updated_at

    ip_dir = _ip_dir(username, profile.id)
    ip_dir.mkdir(parents=True, exist_ok=True)

    p = _ip_json_path(username, profile.id)
    p.write_text(
        json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for char in profile.characters:
        char_dir = _char_dir(username, profile.id, char.id)
        char_dir.mkdir(parents=True, exist_ok=True)
        meta_path = char_dir / "meta.json"
        meta_path.write_text(
            json.dumps(char.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return p


def load_ip(username: str, ip_id: str) -> IPProfile | None:
    """加载单个 IP，不存在返回 None。"""
    p = _ip_json_path(username, ip_id)
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return IPProfile.from_dict(data)


def list_ips(username: str) -> list[IPProfile]:
    """列出用户所有 IP（简要信息）。"""
    base = _ip_base_dir(username)
    if not base.is_dir():
        return []
    profiles: list[IPProfile] = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and (d / "ip.json").is_file():
            ip = load_ip(username, d.name)
            if ip:
                profiles.append(ip)
    return profiles


def delete_ip(username: str, ip_id: str) -> bool:
    """删除 IP 及其所有资产。"""
    ip_dir = _ip_dir(username, ip_id)
    if not ip_dir.is_dir():
        return False
    shutil.rmtree(ip_dir)
    return True


def generate_ip_id() -> str:
    """生成唯一 IP ID。"""
    return uuid.uuid4().hex[:12]


def generate_character_id() -> str:
    """生成唯一角色 ID。"""
    return uuid.uuid4().hex[:8]


def get_character_reference_path(
    username: str, ip_id: str, char_id: str
) -> Path:
    """返回角色参考图的标准存储路径。"""
    return _char_dir(username, ip_id, char_id) / "reference.jpg"


def get_character_voice_path(
    username: str, ip_id: str, char_id: str
) -> Path:
    """返回角色参考音频的标准存储路径。"""
    return _char_dir(username, ip_id, char_id) / "voice_ref.wav"


def save_character_reference_image(
    username: str,
    ip_id: str,
    char_id: str,
    image_path: str | Path,
) -> Path:
    """将图片复制/移动到角色目录下的 reference.jpg，返回目标路径。"""
    src = Path(image_path)
    dest = get_character_reference_path(username, ip_id, char_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src != dest:
        shutil.copy2(src, dest)
    return dest


def update_character_reference_in_profile(
    username: str,
    ip_id: str,
    char_id: str,
    image_path: str,
    ref_type: str = "generated",
) -> IPProfile | None:
    """更新 IP 中某角色的参考图路径并保存。"""
    profile = load_ip(username, ip_id)
    if not profile:
        return None
    char = profile.get_character(char_id)
    if not char:
        return None
    char.reference_image_path = image_path
    char.reference_type = ref_type
    save_ip(username, profile)
    return profile
