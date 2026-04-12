"""从 Shot.dialogue 解析角色对白。

支持的格式：
- 'CharName: "spoken words"'        （分镜标准格式）
- 'CharName: spoken words'           （无引号简化格式）
- 'CharName："说话内容"'              （中文引号）
- '旁白: narration text'             （旁白 / Narrator）
- 多行对白（换行分隔不同角色）
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DialogueLine:
    """一条对白。"""
    speaker: str           # 说话角色名（"Narrator" 表示旁白）
    text: str              # 对白文本（不含引号）
    is_narrator: bool = False

    @property
    def clean_text(self) -> str:
        return self.text.strip().strip('"').strip('"').strip('"').strip("'").strip()


_NARRATOR_NAMES = frozenset({
    "narrator", "旁白", "narration", "voiceover", "vo",
    "画外音", "解说", "内心独白",
})

_DIALOGUE_PATTERN = re.compile(
    r'(?P<speaker>[^:：\u201c\u201d"]+?)'    # 角色名
    r'\s*[：:]\s*'                              # 冒号分隔
    r'(?:'
    r'[\u201c"](?P<q1>.*?)[\u201d"]'            # 双引号包裹（中/英）
    r"|'(?P<q2>.*?)'"                           # 单引号包裹
    r'|(?P<plain>.+?)'                          # 无引号
    r')\s*$',
    re.MULTILINE,
)


def parse_dialogue(raw: str) -> list[DialogueLine]:
    """将 Shot.dialogue 原始文本解析为 DialogueLine 列表。"""
    if not raw or not raw.strip():
        return []

    lines: list[DialogueLine] = []

    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        m = _DIALOGUE_PATTERN.match(line)
        if m:
            speaker = m.group("speaker").strip()
            text = m.group("q1") or m.group("q2") or m.group("plain") or ""
            text = text.strip()
            is_nar = speaker.lower() in _NARRATOR_NAMES
            lines.append(DialogueLine(
                speaker=speaker,
                text=text,
                is_narrator=is_nar,
            ))
        else:
            lines.append(DialogueLine(
                speaker="Narrator",
                text=line,
                is_narrator=True,
            ))

    return lines


def extract_speaking_characters(raw: str) -> list[str]:
    """提取对白中所有说话角色名（去重，保持出现顺序）。"""
    seen: set[str] = set()
    result: list[str] = []
    for dl in parse_dialogue(raw):
        if dl.is_narrator:
            continue
        name = dl.speaker
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result
