"""CosyVoice 预置音色目录 + 统一音色查询接口。

音色列表来源：https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list
仅收录 cosyvoice-v3-flash 支持的常用音色，后续可扩充。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VoicePreset:
    id: str
    name_zh: str
    name_en: str
    gender: str           # male / female / child
    age_group: str        # child / teen / young / middle / elder
    description_zh: str
    tags: list[str]
    provider: str = "cosyvoice"
    model: str = "cosyvoice-v3-flash"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name_zh": self.name_zh,
            "name_en": self.name_en,
            "gender": self.gender,
            "age_group": self.age_group,
            "description_zh": self.description_zh,
            "tags": list(self.tags),
            "provider": self.provider,
            "model": self.model,
        }


# ---------------------------------------------------------------------------
# CosyVoice v3 flash 预置音色
# ---------------------------------------------------------------------------

_COSYVOICE_PRESETS: list[VoicePreset] = [
    # ── 男声 ──
    VoicePreset("longanyang", "龙安洋", "Long Anyang", "male", "young",
                "阳光大男孩", ["阳光", "温暖", "清澈"]),
    VoicePreset("longshu_v3", "龙书", "Long Shu", "male", "young",
                "沉稳青年男", ["沉稳", "磁性", "叙事"]),
    VoicePreset("longshuo_v3", "龙硕", "Long Shuo", "male", "young",
                "博才干练男", ["干练", "利落", "专业"]),
    VoicePreset("longjielidou_v3", "龙杰力豆", "Long Jielidou", "male", "teen",
                "阳光顽皮男", ["顽皮", "活泼", "元气"]),
    VoicePreset("longxiaochun", "龙小纯", "Long Xiaochun", "male", "young",
                "温柔男友音", ["温柔", "治愈", "低沉"]),
    VoicePreset("longxiaoxia", "龙小夏", "Long Xiaoxia", "male", "young",
                "活力男声", ["明亮", "活力", "叙事"]),
    VoicePreset("longlaotie", "龙老铁", "Long Laotie", "male", "middle",
                "东北大哥音", ["豪爽", "搞笑", "接地气"]),
    VoicePreset("longdeshu_v3", "龙德叔", "Long Deshu", "male", "elder",
                "慈祥老者音", ["慈祥", "厚重", "旁白"]),
    # ── 女声 ──
    VoicePreset("longanhuan", "龙安欢", "Long Anhuan", "female", "young",
                "欢脱元气女", ["元气", "甜美", "活泼"]),
    VoicePreset("longling_v3", "龙玲", "Long Ling", "female", "teen",
                "稚气呆萌女", ["可爱", "呆萌", "软糯"]),
    VoicePreset("longxian_v3", "龙仙", "Long Xian", "female", "young",
                "豪放可爱女", ["豪放", "可爱", "利落"]),
    VoicePreset("longanran_v3", "龙安燃", "Long Anran", "female", "young",
                "活泼质感女", ["质感", "活泼", "温暖"]),
    VoicePreset("longanxuan_v3", "龙安宣", "Long Anxuan", "female", "young",
                "经典直播女", ["甜美", "专业", "直播"]),
    VoicePreset("loongbella_v3", "Bella3.0", "Bella 3.0", "female", "young",
                "精准干练女", ["干练", "精准", "职业"]),
    VoicePreset("longdaiyu_v3", "龙黛玉", "Long Daiyu", "female", "young",
                "娇柔才女音", ["柔美", "古典", "文艺"]),
    VoicePreset("longyue_v3", "龙悦", "Long Yue", "female", "young",
                "温婉知性女", ["知性", "温婉", "旁白"]),
    VoicePreset("longmiao_v3", "龙喵", "Long Miao", "female", "teen",
                "软萌猫系女", ["软萌", "甜美", "撒娇"]),
    # ── 童声 ──
    VoicePreset("longshanshan_v3", "龙闪闪", "Long Shanshan", "child", "child",
                "戏剧化童声", ["童声", "戏剧", "夸张"]),
    VoicePreset("longpaopao_v3", "龙泡泡", "Long Paopao", "child", "child",
                "飞天泡泡音", ["童声", "可爱", "清脆"]),
]


def get_all_voice_presets() -> list[dict[str, Any]]:
    """按性别分组返回所有预置音色。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for v in _COSYVOICE_PRESETS:
        label = {"male": "男声", "female": "女声", "child": "童声"}.get(v.gender, v.gender)
        groups.setdefault(label, []).append(v.to_dict())
    return [{"category": k, "voices": vs} for k, vs in groups.items()]


def get_voice_by_id(voice_id: str) -> VoicePreset | None:
    for v in _COSYVOICE_PRESETS:
        if v.id == voice_id:
            return v
    return None


def search_voices(query: str) -> list[dict[str, Any]]:
    """简单搜索：匹配名称或标签。"""
    q = query.lower()
    results = []
    for v in _COSYVOICE_PRESETS:
        if (q in v.name_zh.lower() or q in v.name_en.lower()
                or q in v.description_zh.lower()
                or any(q in t for t in v.tags)):
            results.append(v.to_dict())
    return results
