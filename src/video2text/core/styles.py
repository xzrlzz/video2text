"""风格预设系统：从万相官方 Prompt 指南提取的分类风格关键词。

每个预设包含中英文关键词、说明、适用场景和示例 prompt 片段，
供 IP 创建流程和前端风格选择器使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StylePreset:
    id: str
    category: str
    category_zh: str
    name_zh: str
    name_en: str
    keywords_zh: str
    keywords_en: str
    description_zh: str
    suitable_for: str
    example_prompt_zh: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "category_zh": self.category_zh,
            "name_zh": self.name_zh,
            "name_en": self.name_en,
            "keywords_zh": self.keywords_zh,
            "keywords_en": self.keywords_en,
            "description_zh": self.description_zh,
            "suitable_for": self.suitable_for,
            "example_prompt_zh": self.example_prompt_zh,
        }


# ---------------------------------------------------------------------------
# 写实 (realistic)
# ---------------------------------------------------------------------------

_REALISTIC: list[StylePreset] = [
    StylePreset(
        id="realistic_photo",
        category="realistic",
        category_zh="写实",
        name_zh="写实摄影",
        name_en="Realistic Photography",
        keywords_zh="超写实, 高清细节, 自然光线, 真实质感, 专业级调色, 浅景深",
        keywords_en="photorealistic, high detail, natural lighting, authentic texture, professional color grading, shallow depth of field",
        description_zh="接近真实照片的风格，适合写实人物、风景、产品",
        suitable_for="真人IP, 宠物IP, 美食IP, 产品展示",
        example_prompt_zh="篮子，葡萄，野餐布，超写实静物摄影，微距镜头，丁达尔效应。",
    ),
    StylePreset(
        id="cinematic_portrait",
        category="realistic",
        category_zh="写实",
        name_zh="电影人像",
        name_en="Cinematic Portrait",
        keywords_zh="电影质感, 电影级光照, 35mm胶片, 浅景深, 自然柔和光线, 高清人像",
        keywords_en="cinematic look, film lighting, 35mm film, shallow depth of field, soft natural light, high-resolution portrait",
        description_zh="电影海报级人像质感，强调光影氛围",
        suitable_for="真人IP, 情感向IP, 故事型IP",
        example_prompt_zh="25岁中国女孩，圆脸，看着镜头，优雅的民族服装，商业摄影，室外，电影级光照，半身特写，精致的淡妆。",
    ),
    StylePreset(
        id="documentary",
        category="realistic",
        category_zh="写实",
        name_zh="纪录片风格",
        name_en="Documentary Style",
        keywords_zh="纪录片风格, 抓拍, 自然光, 真实感, 手持镜头质感",
        keywords_en="documentary style, candid shot, natural light, authentic feel, handheld camera look",
        description_zh="随手一拍的抓拍风格，自然不做作",
        suitable_for="日常生活IP, 旅行IP, Vlog向IP",
        example_prompt_zh="自然随性的自拍风格，超高清写实人物生活照，自然柔和的阳光，拍摄机位为人物手持设备的中景自拍视角。",
    ),
]

# ---------------------------------------------------------------------------
# 3D 卡通 (cartoon_3d)
# ---------------------------------------------------------------------------

_CARTOON_3D: list[StylePreset] = [
    StylePreset(
        id="cartoon_3d_cute",
        category="cartoon_3d",
        category_zh="3D卡通",
        name_zh="3D Q版卡通",
        name_en="3D Cute Cartoon",
        keywords_zh="3D卡通风格, Q版, 圆润造型, 明快鲜艳色彩, 柔和光线, 皮克斯风格",
        keywords_en="3D cartoon style, chibi, rounded shapes, vibrant colors, soft lighting, Pixar style",
        description_zh="圆润可爱的 3D 角色，色彩鲜艳，适合搞笑/治愈/儿童向",
        suitable_for="拟人化动物IP, 搞笑日常IP, 儿童向IP",
        example_prompt_zh="网球女运动员，短发，白色网球服，黑色短裤，侧身回球，3D卡通风格。",
    ),
    StylePreset(
        id="clay_style",
        category="cartoon_3d",
        category_zh="3D卡通",
        name_zh="黏土动画",
        name_en="Claymation",
        keywords_zh="粘土风格, 柔和光线, 微缩模型, 可爱, 手工质感, 定格动画",
        keywords_en="claymation style, soft lighting, miniature model, cute, handcrafted texture, stop motion",
        description_zh="像黏土手工捏制的角色和场景，温暖的手工质感",
        suitable_for="治愈向IP, 美食IP, 手工感IP",
        example_prompt_zh="粘土风格，蓝色毛衣的小男孩，棕色卷发，深蓝色贝雷帽，画板，户外，海边，半身照。",
    ),
    StylePreset(
        id="felt_style",
        category="cartoon_3d",
        category_zh="3D卡通",
        name_zh="毛毡风格",
        name_en="Felt Style",
        keywords_zh="毛毡风格, 羊毛毡, 毛毡材质, 可爱, 柔和, 手工质感",
        keywords_en="felt style, wool felt, felt texture, cute, soft, handcrafted texture",
        description_zh="羊毛毡手工质感，毛绒绒的温暖感觉",
        suitable_for="萌宠IP, 治愈向IP, 儿童向IP",
        example_prompt_zh="由羊毛毡制成的大熊猫，头戴大檐帽，穿着蓝色警服马甲，大步奔跑姿态，毛毡效果。",
    ),
    StylePreset(
        id="c4d_render",
        category="cartoon_3d",
        category_zh="3D卡通",
        name_zh="C4D 风格",
        name_en="C4D Render",
        keywords_zh="C4D渲染, 3D立体, 辛烷值渲染, 超高清晰度, 高品质",
        keywords_en="C4D render, 3D, Octane render, ultra high definition, high quality",
        description_zh="专业级 3D 渲染效果，光影精致",
        suitable_for="科技IP, 品牌IP, 精品角色IP",
        example_prompt_zh="中国龙，可爱的中国龙睡在白云上，迷人的花园，在晨雾中，特写，正面，3D立体，C4D渲染。",
    ),
    StylePreset(
        id="cartoon_3d_realistic",
        category="cartoon_3d",
        category_zh="3D卡通",
        name_zh="3D 写实卡通",
        name_en="3D Realistic Cartoon",
        keywords_zh="3D卡通, 写实纹理, 精致渲染, 电影级光照, 迪士尼风格",
        keywords_en="3D cartoon, realistic texture, refined rendering, cinematic lighting, Disney style",
        description_zh="介于卡通与写实之间，类似迪士尼/梦工厂的 3D 动画风格",
        suitable_for="故事型IP, 冒险IP, 家庭向IP",
        example_prompt_zh="3D卡通风格，一位戴着眼镜的年轻冒险家，背着背包站在山顶，风吹动头发，迪士尼风格，电影级光照。",
    ),
]

# ---------------------------------------------------------------------------
# 2D 动漫 (anime_2d)
# ---------------------------------------------------------------------------

_ANIME_2D: list[StylePreset] = [
    StylePreset(
        id="anime_cel",
        category="anime_2d",
        category_zh="2D动漫",
        name_zh="赛璐璐动漫",
        name_en="Cel Animation",
        keywords_zh="赛璐璐风格, 日系动漫, 干净线条, 明亮色彩, 动漫角色设计",
        keywords_en="cel shading, anime style, clean linework, bright colors, anime character design",
        description_zh="经典日系动漫风格，干净的线条与色块",
        suitable_for="二次元IP, 校园IP, 热血/恋爱向IP",
        example_prompt_zh="日系动漫风格，蓝色长发少女，樱花飘落的校园走廊，赛璐璐风格，干净线条，明亮色彩。",
    ),
    StylePreset(
        id="illustration",
        category="anime_2d",
        category_zh="2D动漫",
        name_zh="手绘插画",
        name_en="Hand-drawn Illustration",
        keywords_zh="手绘插画风格, 细腻笔触, 柔和色彩, 温暖色调, 绘本风格",
        keywords_en="hand-drawn illustration, delicate brushwork, soft colors, warm tones, storybook style",
        description_zh="温暖柔和的手绘风格，适合绘本和治愈类内容",
        suitable_for="治愈向IP, 绘本IP, 儿童教育IP",
        example_prompt_zh="手绘插画风格，一只小兔子坐在蘑菇房子前看书，柔和色彩，温暖色调，绘本风格。",
    ),
    StylePreset(
        id="chinese_ink_anime",
        category="anime_2d",
        category_zh="2D动漫",
        name_zh="国风水墨动漫",
        name_en="Chinese Ink Anime",
        keywords_zh="国风水墨风格, 古风, 水墨动漫, 飘逸, 中国风, 淡雅色彩",
        keywords_en="Chinese ink wash anime, ancient Chinese style, ink animation, flowing, Chinese aesthetic, subtle colors",
        description_zh="融合水墨元素的古风动漫，飘逸灵动",
        suitable_for="古风IP, 武侠IP, 国风IP",
        example_prompt_zh="国风水墨风格，一个长长黑发的男人，金色的发簪，飞舞着金色的蝴蝶，白色的服装，深蓝色背景，水墨竹林。",
    ),
    StylePreset(
        id="flat_illustration",
        category="anime_2d",
        category_zh="2D动漫",
        name_zh="扁平插画",
        name_en="Flat Illustration",
        keywords_zh="扁平插画, 几何化造型, 简洁色块, 现代设计感, 矢量风格",
        keywords_en="flat illustration, geometric shapes, simple color blocks, modern design, vector style",
        description_zh="简洁现代的扁平化设计风格",
        suitable_for="科普IP, 商务IP, 信息可视化IP",
        example_prompt_zh="扁平插画风格，简洁色块，一位上班族在咖啡厅办公，现代设计感，矢量风格。",
    ),
]

# ---------------------------------------------------------------------------
# 艺术 (artistic)
# ---------------------------------------------------------------------------

_ARTISTIC: list[StylePreset] = [
    StylePreset(
        id="ink_wash",
        category="artistic",
        category_zh="艺术",
        name_zh="水墨画",
        name_en="Ink Wash Painting",
        keywords_zh="水墨画, 留白, 意境, 细腻笔触, 宣纸纹理, 国风",
        keywords_en="Chinese ink wash painting, negative space, artistic mood, delicate brushwork, rice paper texture",
        description_zh="传统中国水墨画风格，讲究留白与意境",
        suitable_for="古风IP, 国风IP, 禅意IP",
        example_prompt_zh="兰花，水墨画，留白，意境，吴冠中风格，细腻的笔触，宣纸的纹理。",
    ),
    StylePreset(
        id="oil_painting",
        category="artistic",
        category_zh="艺术",
        name_zh="油画",
        name_en="Oil Painting",
        keywords_zh="油画风格, 厚重笔触, 丰富色彩, 经典构图, 明暗对比",
        keywords_en="oil painting style, thick brushwork, rich colors, classical composition, chiaroscuro",
        description_zh="经典油画风格，色彩浓郁，笔触厚重",
        suitable_for="艺术IP, 文化IP, 历史IP",
        example_prompt_zh="油画风格，一位戴着草帽的农夫在麦田里劳作，金色阳光，厚重笔触，丰富色彩。",
    ),
    StylePreset(
        id="watercolor",
        category="artistic",
        category_zh="艺术",
        name_zh="水彩",
        name_en="Watercolor",
        keywords_zh="水彩风格, 透明感, 柔和晕染, 明亮色彩, 梦幻感",
        keywords_en="watercolor style, translucent, soft blending, bright colors, dreamy",
        description_zh="水彩画的透明感和晕染效果",
        suitable_for="治愈IP, 文艺IP, 风景IP",
        example_prompt_zh="浅水彩，咖啡馆外，明亮的白色背景，更少细节，梦幻，吉卜力工作室。",
    ),
    StylePreset(
        id="pointillism",
        category="artistic",
        category_zh="艺术",
        name_zh="点彩画",
        name_en="Pointillism",
        keywords_zh="点彩画风格, 莫奈感, 清晰笔触, 低饱和度, 莫兰迪色",
        keywords_en="pointillism, Monet-inspired, visible brushstrokes, muted saturation, Morandi palette",
        description_zh="用色点构成画面的印象派风格",
        suitable_for="艺术IP, 文艺IP",
        example_prompt_zh="一座白色的小房子，茅草房，被雪覆盖的草原，大胆使用点彩画，莫奈感，清晰的笔触，莫兰迪色。",
    ),
    StylePreset(
        id="gongbi",
        category="artistic",
        category_zh="艺术",
        name_zh="工笔画",
        name_en="Gongbi Painting",
        keywords_zh="工笔画, 精致细腻, 丝绸质感, 传统国画, 细致勾勒",
        keywords_en="Gongbi painting, meticulous detail, silk-like texture, traditional Chinese painting, fine outline",
        description_zh="中国传统工笔画，极致细腻的描绘",
        suitable_for="古风IP, 国画IP, 花鸟IP",
        example_prompt_zh="晨曦中，一枝寒梅傲立雪中，花瓣细腻如丝，露珠轻挂，展现工笔画之精致美。",
    ),
    StylePreset(
        id="pixel_art",
        category="artistic",
        category_zh="艺术",
        name_zh="像素风格",
        name_en="Pixel Art",
        keywords_zh="像素风格, 复古像素, 8-bit, 游戏感, 方块色块",
        keywords_en="pixel art, retro pixel, 8-bit, game aesthetic, blocky colors",
        description_zh="复古像素游戏风格",
        suitable_for="游戏IP, 复古IP, 怀旧IP",
        example_prompt_zh="像素风格，一个骑士站在城堡前，8-bit复古像素，方块色块，蓝天绿地。",
    ),
]

# ---------------------------------------------------------------------------
# 特殊风格 (special)
# ---------------------------------------------------------------------------

_SPECIAL: list[StylePreset] = [
    StylePreset(
        id="puppet",
        category="special",
        category_zh="特殊风格",
        name_zh="木偶动画",
        name_en="Puppet Animation",
        keywords_zh="木偶动画, 提线木偶, 手工质感, 定格动画, 舞台感",
        keywords_en="puppet animation, marionette, handcrafted texture, stop motion, theatrical",
        description_zh="木偶戏的独特手工质感",
        suitable_for="童话IP, 怀旧IP, 儿童IP",
        example_prompt_zh="木偶动画风格，提线木偶的小丑在马戏团舞台上表演，舞台灯光，手工质感。",
    ),
    StylePreset(
        id="origami",
        category="special",
        category_zh="特殊风格",
        name_zh="折纸风格",
        name_en="Origami Style",
        keywords_zh="折纸风格, 纸艺, 极简主义, 背光, 精致折痕, 最佳品质",
        keywords_en="origami style, paper art, minimalism, backlit, crisp folds, best quality",
        description_zh="精致的折纸艺术风格",
        suitable_for="文创IP, 教育IP, 环保IP",
        example_prompt_zh="折纸杰作，牛皮纸材质的熊猫，森林背景，中景，极简主义，背光，最佳品质。",
    ),
    StylePreset(
        id="steampunk",
        category="special",
        category_zh="特殊风格",
        name_zh="蒸汽朋克",
        name_en="Steampunk",
        keywords_zh="蒸汽朋克风格, 齿轮机械, 铜质金属, 维多利亚时代, 工业感",
        keywords_en="steampunk style, gears and machinery, brass metal, Victorian era, industrial aesthetic",
        description_zh="蒸汽与齿轮的复古未来主义",
        suitable_for="冒险IP, 科幻IP, 复古IP",
        example_prompt_zh="蒸汽朋克风格，一位戴护目镜的发明家站在齿轮装置前，铜质金属质感，维多利亚时代。",
    ),
    StylePreset(
        id="cyberpunk",
        category="special",
        category_zh="特殊风格",
        name_zh="赛博朋克",
        name_en="Cyberpunk",
        keywords_zh="赛博朋克风格, 霓虹灯光, 高科技低生活, 雨夜, 未来都市, 反差色彩",
        keywords_en="cyberpunk style, neon lights, high-tech low-life, rainy night, futuristic city, contrasting colors",
        description_zh="霓虹灯下的未来都市，高科技与低生活的碰撞",
        suitable_for="科幻IP, 暗黑IP, 未来向IP",
        example_prompt_zh="赛博朋克风格，雨后的城市街景，霓虹灯光在湿润的地面上反射，行人撑伞匆匆走过。",
    ),
    StylePreset(
        id="wasteland",
        category="special",
        category_zh="特殊风格",
        name_zh="废土风",
        name_en="Post-apocalyptic",
        keywords_zh="废土风格, 末日废墟, 荒凉, 锈蚀金属, 沙尘暴, 末世感",
        keywords_en="post-apocalyptic style, ruins, desolate, rusted metal, sandstorm, end-of-world atmosphere",
        description_zh="末世废墟的荒凉美学",
        suitable_for="末日IP, 冒险IP, 科幻IP",
        example_prompt_zh="火星上的城市，废土风格，荒凉的红色沙漠，倒塌的建筑，锈蚀金属。",
    ),
    StylePreset(
        id="surreal",
        category="special",
        category_zh="特殊风格",
        name_zh="超现实",
        name_en="Surrealism",
        keywords_zh="超现实风格, 梦幻, 不可能空间, 视觉错觉, 奇异美感",
        keywords_en="surrealism, dreamlike, impossible space, visual illusion, bizarre beauty",
        description_zh="打破物理规则的梦幻超现实场景",
        suitable_for="艺术IP, 哲学IP, 创意IP",
        example_prompt_zh="深灰色大海中一条粉红色的发光河流，极简美感，超现实风格的电影灯光。",
    ),
    StylePreset(
        id="bw_animation",
        category="special",
        category_zh="特殊风格",
        name_zh="黑白动画",
        name_en="Black & White Animation",
        keywords_zh="黑白动画, 高对比度, 复古, 默片风格, 简洁线条",
        keywords_en="black and white animation, high contrast, retro, silent film style, clean lines",
        description_zh="复古黑白动画，默片时代的怀旧感",
        suitable_for="复古IP, 文艺IP, 默剧IP",
        example_prompt_zh="黑白动画风格，高对比度，一只小狗在街灯下追蝴蝶，默片风格，简洁线条。",
    ),
    StylePreset(
        id="ceramic",
        category="special",
        category_zh="特殊风格",
        name_zh="陶瓷风格",
        name_en="Ceramic Style",
        keywords_zh="瓷器质感, 细腻光泽, 高细节, 白瓷, 精致雕刻",
        keywords_en="ceramic texture, delicate gloss, high detail, white porcelain, refined carving",
        description_zh="精致瓷器质感，细腻光泽",
        suitable_for="文创IP, 国潮IP, 工艺IP",
        example_prompt_zh="高细节的瓷器小狗，它静静地躺在桌上，脖子上系着一个精致的铃铛，瓷器质感。",
    ),
]

# ---------------------------------------------------------------------------
# 影视 (cinematic)
# ---------------------------------------------------------------------------

_CINEMATIC: list[StylePreset] = [
    StylePreset(
        id="cinematic_film",
        category="cinematic",
        category_zh="影视",
        name_zh="电影感",
        name_en="Cinematic Film",
        keywords_zh="电影感, 宽银幕, 21:9画幅, 电影调色, 景深, 镜头光晕, 胶片质感",
        keywords_en="cinematic, widescreen, 21:9 aspect ratio, film color grading, depth of field, lens flare, film grain",
        description_zh="大片感的宽银幕电影画面",
        suitable_for="叙事IP, 大制作IP, 情感IP",
        example_prompt_zh="电影感，宽银幕构图，一位旅人在沙漠中独行，逆光剪影，电影调色，胶片质感。",
    ),
    StylePreset(
        id="tilt_shift",
        category="cinematic",
        category_zh="影视",
        name_zh="移轴摄影",
        name_en="Tilt-shift Photography",
        keywords_zh="移轴摄影, 微缩模型效果, 浅景深, 俯瞰, 玩具感",
        keywords_en="tilt-shift photography, miniature model effect, shallow depth of field, bird-eye view, toy-like",
        description_zh="移轴镜头的微缩世界效果",
        suitable_for="城市IP, 日常IP, 趣味IP",
        example_prompt_zh="移轴摄影效果，俯瞰繁忙的十字路口，微缩模型感，浅景深，色彩鲜艳。",
    ),
    StylePreset(
        id="timelapse",
        category="cinematic",
        category_zh="影视",
        name_zh="延时风格",
        name_en="Timelapse Style",
        keywords_zh="延时摄影, 时间流逝, 光影变化, 动态模糊, 壮观",
        keywords_en="timelapse photography, time-lapse, shifting light and shadow, motion blur, spectacular",
        description_zh="时间流逝的壮观动态感",
        suitable_for="风景IP, 城市IP, 自然IP",
        example_prompt_zh="延时摄影风格，城市天际线从白天到黑夜的光影变化，车流形成的光轨，壮观。",
    ),
]

# ---------------------------------------------------------------------------
# 所有预设的注册表
# ---------------------------------------------------------------------------

_ALL_PRESETS: list[StylePreset] = (
    _REALISTIC + _CARTOON_3D + _ANIME_2D + _ARTISTIC + _SPECIAL + _CINEMATIC
)

_PRESETS_BY_ID: dict[str, StylePreset] = {p.id: p for p in _ALL_PRESETS}

_CATEGORIES = [
    {"category": "realistic", "category_zh": "写实"},
    {"category": "cartoon_3d", "category_zh": "3D卡通"},
    {"category": "anime_2d", "category_zh": "2D动漫"},
    {"category": "artistic", "category_zh": "艺术"},
    {"category": "special", "category_zh": "特殊风格"},
    {"category": "cinematic", "category_zh": "影视"},
]


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def get_all_style_presets() -> list[dict[str, Any]]:
    """返回按分类组织的完整风格列表。"""
    result: list[dict[str, Any]] = []
    for cat in _CATEGORIES:
        styles = [p.to_dict() for p in _ALL_PRESETS if p.category == cat["category"]]
        result.append({
            "category": cat["category"],
            "category_zh": cat["category_zh"],
            "styles": styles,
        })
    return result


def get_style_by_id(style_id: str) -> dict[str, Any] | None:
    """根据 id 获取单个风格预设。"""
    p = _PRESETS_BY_ID.get(style_id)
    return p.to_dict() if p else None


def get_style_keywords(style_id: str, lang: str = "zh") -> str:
    """提取指定风格的关键词字符串。"""
    p = _PRESETS_BY_ID.get(style_id)
    if not p:
        return ""
    return p.keywords_zh if lang == "zh" else p.keywords_en


def search_styles(query: str) -> list[dict[str, Any]]:
    """模糊搜索风格预设（匹配名称、关键词、说明、适用场景）。"""
    q = query.strip().lower()
    if not q:
        return [p.to_dict() for p in _ALL_PRESETS]
    results: list[dict[str, Any]] = []
    for p in _ALL_PRESETS:
        searchable = (
            f"{p.name_zh} {p.name_en} {p.keywords_zh} {p.keywords_en} "
            f"{p.description_zh} {p.suitable_for} {p.category_zh}"
        ).lower()
        if q in searchable:
            results.append(p.to_dict())
    return results


def get_all_style_ids() -> list[str]:
    """返回所有风格 id 列表（用于 LLM prompt 中列出可选项）。"""
    return [p.id for p in _ALL_PRESETS]


def format_styles_for_llm() -> str:
    """将风格列表格式化为 LLM 可读的文本，用于 IP 创建时让 LLM 选择风格。"""
    lines: list[str] = []
    for cat in _CATEGORIES:
        styles = [p for p in _ALL_PRESETS if p.category == cat["category"]]
        lines.append(f"\n[{cat['category_zh']}]")
        for p in styles:
            lines.append(f"  - {p.id}: {p.name_zh} ({p.name_en}) — {p.description_zh}")
    return "\n".join(lines)
