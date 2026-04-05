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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Shot:
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
            audio_description=str(d.get("audio_description", "")),
            generation_prompt=str(d.get("generation_prompt", "")),
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "synopsis": self.synopsis,
            "characters": self.characters,
            "source_video": self.source_video,
            "shots": [s.to_dict() for s in self.shots],
            "raw_scene_analyses": self.raw_scene_analyses,
        }

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
            "## 故事梗概",
            self.synopsis or "（无）",
            "",
            "## 角色与关系",
            self.characters or "（无）",
            "",
            "## 分镜列表",
            "",
        ]
        for s in self.shots:
            lines.append(f"### 镜头 {s.shot_id}（{s.start_time} → {s.end_time}，{s.duration:.1f}s）")
            lines.append(f"- **景别**：{s.shot_type}")
            lines.append(f"- **运镜**：{s.camera_movement}")
            lines.append(f"- **场景**：{s.scene_description}")
            lines.append(f"- **动作**：{s.character_action}")
            lines.append(f"- **对白**：{s.dialogue}")
            lines.append(f"- **氛围 / 光线 / 声音**：{s.mood}；{s.lighting}；{s.audio_description}")
            lines.append("")
        lines.extend(["## 生成用 Prompt（万相）", ""])
        for s in self.shots:
            if s.generation_prompt:
                lines.append(f"### 镜头 {s.shot_id}\n\n{s.generation_prompt}\n")
        return "\n".join(lines)

    def save_markdown(self, path: str | Path) -> None:
        Path(path).write_text(self.to_markdown(), encoding="utf-8")
