"""Storyboard data structures and serialization."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Shot:
    shot_id: int
    start_time: str  # HH:mm:ss
    end_time: str
    duration: float
    shot_type: str
    camera_movement: str
    scene_description: str
    character_action: str
    dialogue: str
    mood: str
    lighting: str
    audio_description: str
    generation_prompt: str = ""
    characters_in_shot: list[str] = field(default_factory=list)
    camera_angle: str = ""
    composition: str = ""
    eyeline_and_screen_direction: str = ""
    continuity_note: str = ""
    continuity_anchor: str = ""
    focal_character: str = ""
    cut_rhythm: str = ""
    negative_prompt_hint: str = ""
    ambient_sound: str = ""
    score_suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d.get("characters_in_shot"):
            d.pop("characters_in_shot", None)
        for k in (
            "camera_angle", "composition", "eyeline_and_screen_direction",
            "continuity_note", "continuity_anchor", "focal_character",
            "cut_rhythm", "negative_prompt_hint", "ambient_sound",
            "score_suggestion",
        ):
            if not d.get(k):
                d.pop(k, None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Shot:
        raw_chars = d.get("characters_in_shot") or []
        if isinstance(raw_chars, str):
            raw_chars = [c.strip() for c in raw_chars.split(",") if c.strip()]
        ambient = str(d.get("ambient_sound", ""))
        audio_desc = str(d.get("audio_description", ""))
        if not audio_desc and ambient:
            audio_desc = ambient
        return cls(
            shot_id=int(d["shot_id"]),
            start_time=str(d.get("start_time", "00:00:00")),
            end_time=str(d.get("end_time", "00:00:00")),
            duration=float(d.get("duration", 0.0)),
            shot_type=str(d.get("shot_type", "")),
            camera_movement=str(d.get("camera_movement", "")),
            scene_description=str(d.get("scene_description", "")),
            character_action=str(d.get("character_action", "")),
            dialogue=str(d.get("dialogue", "")),
            mood=str(d.get("mood", "")),
            lighting=str(d.get("lighting", "")),
            audio_description=audio_desc,
            generation_prompt=str(d.get("generation_prompt", "")),
            characters_in_shot=list(raw_chars),
            camera_angle=str(d.get("camera_angle", "")),
            composition=str(d.get("composition", "")),
            eyeline_and_screen_direction=str(d.get("eyeline_and_screen_direction", "")),
            continuity_note=str(d.get("continuity_note", "")),
            continuity_anchor=str(d.get("continuity_anchor", "")),
            focal_character=str(d.get("focal_character", "")),
            cut_rhythm=str(d.get("cut_rhythm", "")),
            negative_prompt_hint=str(d.get("negative_prompt_hint", "")),
            ambient_sound=ambient,
            score_suggestion=str(d.get("score_suggestion", "")),
        )


@dataclass
class StoryboardDocument:
    """Full storyboard with global narrative metadata."""

    title: str = ""
    synopsis: str = ""
    characters: str = ""
    source_video: str = ""
    shots: list[Shot] = field(default_factory=list)
    raw_scene_analyses: list[str] = field(default_factory=list)
    logline: str = ""
    scene_geography: str = ""
    pacing_flow: str = ""
    rhythm_profile: str = ""
    ip_char_ref_map: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "title": self.title,
            "synopsis": self.synopsis,
            "characters": self.characters,
            "source_video": self.source_video,
            "shots": [s.to_dict() for s in self.shots],
            "raw_scene_analyses": self.raw_scene_analyses,
        }
        for k, v in (
            ("logline", self.logline),
            ("scene_geography", self.scene_geography),
            ("pacing_flow", self.pacing_flow),
            ("rhythm_profile", self.rhythm_profile),
        ):
            if v:
                d[k] = v
        if self.ip_char_ref_map:
            d["ip_char_ref_map"] = self.ip_char_ref_map
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoryboardDocument:
        shots = [Shot.from_dict(x) for x in d.get("shots", [])]
        return cls(
            title=str(d.get("title", "")),
            synopsis=str(d.get("synopsis", "")),
            characters=str(d.get("characters", "")),
            source_video=str(d.get("source_video", "")),
            shots=shots,
            raw_scene_analyses=list(d.get("raw_scene_analyses", [])),
            logline=str(d.get("logline", "")),
            scene_geography=str(d.get("scene_geography", "")),
            pacing_flow=str(d.get("pacing_flow", "")),
            rhythm_profile=str(d.get("rhythm_profile", "")),
            ip_char_ref_map=dict(d.get("ip_char_ref_map", {})),
        )

    def save_json(self, path: str | Path) -> None:
        p = Path(path)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: str | Path) -> StoryboardDocument:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def to_markdown(self) -> str:
        lines = [
            f"# {self.title or '分镜脚本'}",
            "",
        ]
        if self.logline:
            lines += ["## Logline", self.logline, ""]
        lines += ["## 故事梗概", self.synopsis or "（无）", ""]
        if self.scene_geography:
            lines += ["## 场景空间", self.scene_geography, ""]
        lines += ["## 角色与关系", self.characters or "（无）", ""]
        if self.pacing_flow:
            lines += ["## 节奏", self.pacing_flow, ""]
        if self.rhythm_profile:
            lines += [f"**Rhythm Profile**: {self.rhythm_profile}", ""]
        lines += ["## 分镜列表", ""]
        for s in self.shots:
            lines.append(f"### 镜头 {s.shot_id}（{s.start_time} → {s.end_time}，{s.duration:.1f}s）")
            if s.characters_in_shot:
                lines.append(f"- **出场角色**：{', '.join(s.characters_in_shot)}")
            if s.focal_character:
                lines.append(f"- **焦点角色**：{s.focal_character}")
            lines.append(f"- **景别**：{s.shot_type}")
            if s.camera_angle:
                lines.append(f"- **机位角度**：{s.camera_angle}")
            lines.append(f"- **运镜**：{s.camera_movement}")
            if s.composition:
                lines.append(f"- **构图**：{s.composition}")
            lines.append(f"- **场景**：{s.scene_description}")
            lines.append(f"- **动作**：{s.character_action}")
            lines.append(f"- **对白**：{s.dialogue}")
            if s.eyeline_and_screen_direction:
                lines.append(f"- **视线与屏幕方向**：{s.eyeline_and_screen_direction}")
            audio = f"{s.mood}；{s.lighting}；{s.audio_description}"
            lines.append(f"- **氛围 / 光线 / 声音**：{audio}")
            if s.score_suggestion:
                lines.append(f"- **配乐建议**：{s.score_suggestion}")
            if s.cut_rhythm:
                lines.append(f"- **剪辑节奏**：{s.cut_rhythm}")
            if s.continuity_anchor:
                lines.append(f"- **连续性锚点**：{s.continuity_anchor}")
            if s.continuity_note:
                lines.append(f"- **剪辑逻辑**：{s.continuity_note}")
            if s.negative_prompt_hint:
                lines.append(f"- **负面提示**：{s.negative_prompt_hint}")
            lines.append("")
        lines.extend(["## 生成用 Prompt（万相）", ""])
        for s in self.shots:
            if s.generation_prompt:
                lines.append(f"### 镜头 {s.shot_id}\n\n{s.generation_prompt}\n")
        return "\n".join(lines)

    def save_markdown(self, path: str | Path) -> None:
        Path(path).write_text(self.to_markdown(), encoding="utf-8")
