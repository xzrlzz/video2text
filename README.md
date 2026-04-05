# video2text — 视频分镜脚本与万相再生成

基于阿里云百炼 **DashScope**：用 **通义千问 3.6+**（`qwen3.6-plus`）做视频理解，用 **通义万相 2.6**（`wan2.6-t2v`）按分镜生成新视频。

## 环境要求

- Python 3.10+（推荐；3.9 亦可尝试）
- 系统已安装 **ffmpeg** 并在 `PATH` 中
- 阿里云百炼 **API Key**（[获取说明](https://help.aliyun.com/zh/model-studio/get-api-key)），通过下面 **配置文件** 或环境变量提供

## 安装

```bash
cd video2text
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Web 界面（本地）

黑色主题单页 + Flask API，封装主题/视频分析、参考图上传、分镜编辑、万相生成与 **workspace 断点缓存**（`workspace/<任务ID>/`）。

```bash
# 在项目根目录，已安装依赖并配置好 config.json（或首次启动会从 config.example.json 复制）
python app.py
```

浏览器打开 **http://127.0.0.1:5000**。在「设置」中填写/保存 API Key 与模型；「新建任务」后按步骤操作。生成中断后同一任务可再点「开始/继续生成」复用已下载的 `segments/seg_*.mp4`。

## 配置文件（推荐）

1. 将仓库中的 [`config.example.json`](config.example.json) 复制为同目录下的 **`config.json`**。
2. 在 `config.json` 里填写 **`dashscope_api_key`**（`sk-` 开头），按需修改模型名、分辨率等字段。
3. **`config.json` 已加入 `.gitignore`，请勿提交到 Git。**

查找顺序（未指定 `--config` 时）：环境变量 **`V2T_CONFIG`** 指向的文件 → 当前工作目录下的 `config.json` → 本项目目录下的 `config.json`。

也可在命令行指定：

```bash
python video2text.py --config /path/to/my-config.json analyze --input ...
```

**优先级**：若同时设置了环境变量与配置文件，**环境变量覆盖配置文件**中的同名字段（例如 `DASHSCOPE_API_KEY` 覆盖 `dashscope_api_key`）。

生成相关可选字段（见 `config.example.json`）：`subject_descriptions`、`reference_urls`、`reference_video_urls`、`reference_video_descriptions`、`max_segment_seconds`。命令行传入的主体会**追加**在配置文件条目之后。

## 用法

```bash
# 已配置 config.json 或 export DASHSCOPE_API_KEY=sk-... 时可省略密钥参数

# 仅生成分镜 JSON（默认：整支视频一次送入模型，便于全局分镜与剧本连贯性）
python video2text.py analyze --input ./clip.mp4 --output ./storyboard.json --markdown ./storyboard.md

# 可选：按镜头自动切片并多次调用模型（旧模式，需 PySceneDetect + ffmpeg 切片）
python video2text.py analyze --input ./clip.mp4 --output ./sb.json --segment-scenes

# 公网 HTTPS 视频 URL：同样整片一次分析
python video2text.py analyze --output ./sb.json --video-url 'https://example.com/video.mp4'

# 根据分镜 JSON 调用万相生成并拼接（若分镜总时长超过单次上限会自动多段生成后拼接）
python video2text.py generate --storyboard ./storyboard.json --output ./out.mp4

# 指定多个主体文字 + 本地参考图（跨段一致；参考图由 SDK 上传）
python video2text.py generate --storyboard ./sb.json --output ./out.mp4 \
  --subject "主角：成年男性，深灰西装" --subject "配角：机械犬，银色外壳" \
  --reference-image ./ref_hero.png --reference-image ./ref_dog.png

# 一步：分析 + 生成（中间分镜默认写入与输出同名的 .storyboard.json）
python video2text.py run --input ./clip.mp4 --output ./out.mp4 --style "水彩插画风格"
```

### 从主题文本生成分镜（再接万相）

无需源视频时，可用大模型根据**主题/创意**直接写故事并拆成与 `analyze` **同一套 JSON 分镜**（含每镜 `dialogue` 角色对白），再交给 `generate`：

```bash
# 1）主题 → 分镜 JSON（默认 qwen 文本模型，可用 config 中 theme_story_model 覆盖）
python video2text.py theme --theme "两个陌生人在末班地铁上因一把伞相识" -o ./my_story.json --markdown ./my_story.md

# 主流程一步：主题 → 分镜 → 万相（与 generate 相同，须参考图等）
python video2text.py run --theme "末班地铁上一把伞" -o ./out.mp4 --reference-image ./角色1.png

# 主题较长时可写进文件
python video2text.py theme --theme-file ./idea.txt -o ./my_story.json

# 2a）参考生：参考图 + generate
python video2text.py generate --storyboard ./my_story.json -o ./out.mp4 \
  --reference-image ./角色1.png --reference-image ./角色2.png

# 2b）纯文生（不要参考图）：加 --text-only-video（勿用 --require-reference "false"，那是无效写法）
python video2text.py generate --storyboard ./my_story.json -o ./out.mp4 --text-only-video
```

可选：`--min-shots` / `--max-shots` 控制镜头数量；`--model` 指定文本模型；环境变量 `V2T_THEME_MODEL` 或配置项 `theme_story_model` 可默认专用 cheaper 文本模型（留空则与 `vision_model` 相同）。

### 常用参数

| 命令 | 参数 | 说明 |
|------|------|------|
| `analyze` | `--style` | 理解与整合阶段的风格/改编提示 |
| `analyze` | `--segment-scenes` | 启用切片 + 多次调用；默认关闭（整片一次理解） |
| `analyze` | `--threshold` | 仅与 `--segment-scenes` 联用：PySceneDetect 阈值 |
| `analyze` | `--work-dir` | 仅与 `--segment-scenes` 联用：切片缓存目录 |
| `analyze` | `--skip-consolidate` | 跳过第二次「叙事整合」文本调用 |
| `theme` | `--theme` / `--theme-file` | 故事主题；可合用（文件内容与命令行拼接） |
| `theme` | `--min-shots` / `--max-shots` | 镜头数量范围（默认 8～24） |
| `theme` | `--model` | 创作用文本模型；省略则用 `theme_story_model` 或 `vision_model` |
| `generate` | `--resolution` | 如 `1280*720`、`1920*1080` |
| `generate` | `--workers` | 并行生成段数（参考生每段≤10s，文生≤15s） |
| `generate` | `--update-storyboard` | 写回各镜头的 `generation_prompt` |
| `generate` | `--subject`（可多次） | 主体文字设定，写入每一段万相 prompt |
| `generate` | `--subjects-file` | 每行一条主体描述（`#` 行为注释） |
| `generate` | `--reference-image` / `--reference-url` | 万相参考图（本地路径或 HTTPS，可多个） |
| `generate` | `--reference-video` + `--reference-video-desc` | 参考视频与说明（数量须一一对应） |
| `generate` | `--max-segment-seconds` | 切段阈值；文生段长≤15s，参考生≤10s |
| `generate` | `--require-reference` | 配置里关了 `require_reference` 时，仍强制要参考素材 |
| `generate` | `--text-only-video` | **纯文生（t2v）**：不要参考图/视频，一条命令即可（推荐） |
| `generate` | `--no-require-reference` | 仅在 `require_reference` 已为 `false` 时生效，本次允许无参考走文生（t2v） |
| `generate` | `--storyboard` | 须为 **JSON**（`.json`），不能传 `.md` |
| `run` | `--theme` / `--theme-file` | 无视频时：先主题创作为分镜再 generate（与 `--input` 互斥） |
| `run` | `--min-shots` / `--max-shots` / `--theme-model` | 仅主题模式：镜头范围与创作模型 |
| `run` | `--keep-storyboard` | 指定中间 JSON 路径 |

### 主体一致性与「参考生」模型（万相）

阿里云把能力分成两类（详见 [万相-参考生视频 API](https://help.aliyun.com/zh/model-studio/wan-video-to-video-api-reference)）：

| 类型 | 代表模型 | 除文本外的主体参考 |
|------|-----------|-------------------|
| **文生视频（t2v）** | `wan2.6-t2v`、`wan2.7-t2v` 等 | **不支持**用图/视频锁定主体，仅靠 prompt |
| **参考生视频（r2v）** | **wan2.7**：`wan2.7-r2v`（HTTP，`input.media`） | **支持**参考图、参考视频等 |
| **参考生视频（r2v）** | **wan2.6**：`wan2.6-r2v`、`wan2.6-r2v-flash`（旧版 HTTP/SDK 参数） | **支持**多路 URL 参考 |

本项目：**生成阶段默认一律参考生**：须提供参考图或参考视频，使用 `video_ref_model`（默认 `wan2.7-r2v`）。**仅在**先将 `require_reference` 设为 `false`，**且**本次命令加上 `--no-require-reference` 时，才允许在无参考素材下使用文生 `video_gen_model`（如 `wan2.7-t2v`）；单独加 `--no-require-reference` 无法绕过默认策略。

### 参考图怎么传、两个角色怎么对应「谁是谁」

1. **配置文件** `config.json`：在 `reference_urls` 里写 HTTPS 图片地址（字符串数组，**顺序即万相里的编号顺序**）。
2. **命令行**：可多次传入  
   - `--reference-url`（公网图 URL）  
   - `--reference-image`（本地图片路径）  
   会与配置里的列表**按顺序拼接**：先是配置里的 URL，再是 CLI 的 `--reference-url`，最后是 `--reference-image`（见 `video2text._merge_reference_urls`）。
3. **与视频同时存在时（wan2.7-r2v）**：接口里**先全部参考视频、再全部参考图**。编号为 **视频1、视频2…** 然后 **图1、图2…**。没有参考视频时，只有 **图1、图2…**。
4. **指定哪个图是哪个角色**：万相要求「每张参考图里尽量只有**一个**角色」，并在 **prompt 里用文字指代**。做法是：  
   - 在 **`subject_descriptions`**（或 `--subject`）里写清对应关系，例如两条：`图1：女主角××，短发黑衣`；`图2：男主角××，戴眼镜`（或与 `主体1`/`主体2` 同序，与上图顺序一致）。  
   - 在分镜 **`generation_prompt`**（或分析后手改 storyboard）里写镜头时显式写 **「图1」「图2」**（wan2.7）或 **character1、character2**（wan2.6-r2v），如：「图1与图2在咖啡馆对坐，图1看向窗外」。  
   模型没有单独的「角色 ID 绑定」字段，**顺序 + prompt 指代**就是官方约定方式。

生成前终端会打印本次参考视频/图片个数，便于核对 **图1、图2** 是否与你的文件顺序一致。

**多段生成时**：参考图/视频会在第一段任务前**统一上传/解析为 URL**，之后每一段万相请求**复用同一批 URL**，避免并行重复上传导致的不一致；多主体时请传**多张图**（或**多个参考视频**），并在 prompt 里用 **图1 / 图2**（或 character 序号）指代。

可选环境变量（覆盖配置文件）：`DASHSCOPE_API_KEY`、`V2T_VISION_MODEL`、`V2T_THEME_MODEL`（主题创作用文本模型）、`V2T_GEN_MODEL`、`V2T_REF_MODEL`（参考生模型）、`V2T_RESOLUTION`、`DASHSCOPE_HTTP_BASE` / `V2T_BASE_URL`、`V2T_CONFIG`（配置文件路径）、`V2T_SCENE_THRESHOLD`、`V2T_ANALYSIS_FPS` 等。

## 说明与限制

- **默认整片分析**：本地文件若小于配置的 Base64 上限，走 OpenAI 兼容接口 + Data URL；否则自动改用 DashScope **本地文件路径** 单次上传理解。公网 **HTTPS URL** 同样为单次调用。
- **超长视频**可能受模型上下文、时长与费用限制；若单次失败可尝试先压缩/截短，或使用 `--segment-scenes` 分段（会削弱全局连贯性）。
- 千问**不分析视频中的音频**；对白依赖口型/字幕推断。
- 万相**文生**单次常见最长 **15 秒**；**参考生（r2v）**单次常见最长 **10 秒**；长片会按上限切段多次生成再 **ffmpeg 拼接**。
- 生成结果 URL 约 **24 小时**有效，脚本会自动下载到本地再拼接。

## 测试

```bash
python3 -m unittest tests.test_storyboard -v
```

端到端流程需有效 API Key 与配额，请自行用短视频验证。

## 项目结构

- `app.py` — Web UI（Flask）与 `static/index.html`
- `video2text.py` — CLI
- `config.py` — 配置与 `load_settings()`
- `scene_detector.py` — 场景检测、切片、关键帧
- `video_analyzer.py` — 千问视频理解 + 叙事整合
- `story_from_theme.py` — 主题文本 → 故事分镜 + 对白
- `storyboard.py` — 分镜数据结构 / JSON / Markdown
- `video_generator.py` — 万相文生视频、下载
- `video_composer.py` — ffmpeg 拼接
