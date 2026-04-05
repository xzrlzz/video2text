# video2text — 视频分镜脚本与万相再生成

基于阿里云百炼 **DashScope**：用 **通义千问 3.6+**（`qwen3.6-plus`）做视频理解，用 **通义万相**（如 `wan2.7-t2v` / `wan2.7-r2v`）按分镜生成新视频。

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
pip install -e .
```

`pip install -e .` 会注册命令行入口 **`v2t`**（CLI）与 **`v2t-web`**（Web UI），并把 `video2text` 包装到当前环境。

若你使用 **Miniconda/Anaconda**，可激活已有环境（例如名为 **`video`** 的环境）后同样执行 `pip install -r requirements.txt` 与 `pip install -e .`。运行测试示例：

```bash
conda activate video
cd video2text
pip install -e .
python -m unittest discover -s tests -v
```

若不安装包，也可在仓库根目录使用：

```bash
export PYTHONPATH=src   # Windows: set PYTHONPATH=src
python cli/main.py --help
python -m video2text.web.app
```

## 目录结构

```
video2text/
├── pyproject.toml              # 包元数据与 v2t / v2t-web 入口
├── config.json                 # 本地密钥（gitignored，从模板复制）
├── cli/                        # 未安装时的入口脚本
├── src/video2text/             # 主包
│   ├── config/                 # Settings、配置加载
│   ├── core/                   # 分镜模型、视频分析、场景检测、主题创作
│   ├── services/               # 万相 HTTP、参考媒体规范化
│   ├── pipeline/               # 生成编排、ffmpeg 拼接
│   ├── web/                    # Flask API
│   └── utils/                  # 路径解析
├── data/
│   ├── config/
│   │   └── config.example.json # 配置模板（tracked）
│   ├── input/                  # 输入素材（gitignored）
│   ├── output/                 # 输出结果（gitignored）
│   └── workspace/              # Web 任务缓存（gitignored）
├── static/                     # Web 前端
└── tests/
```

## 架构说明

代码按分层组织（详见 [`src/video2text/__init__.py`](src/video2text/__init__.py)）：

| 层级 | 包路径 | 职责 |
|------|--------|------|
| 入口 | `video2text.cli` | Click：`analyze` / `theme` / `generate` / `run` |
| 入口 | `video2text.web` | Flask：配置 API、任务、静态页、断点续传 |
| 编排 | `video2text.pipeline` | 万相切段与参考筛选（Web/CLI 共用）、ffmpeg 拼接 |
| 领域 | `video2text.core` | 分镜模型、视频分析、场景检测、主题创作 |
| 服务 | `video2text.services` | 万相 HTTP、参考媒体规范化 |
| 配置 | `video2text.config` | `Settings`、`load_settings`、JSON 配置 |
| 工具 | `video2text.utils` | 项目根、`workspace` / `static` / `data` 路径 |

路径与可选环境变量：

- **`V2T_WORKSPACE`** — Web 任务目录根路径，默认 `data/workspace/`。
- **`V2T_STATIC`** — 静态资源目录，默认 `<项目根>/static`。
- **`V2T_CONFIG`** — 显式指定配置文件（CLI 未传 `--config` 时参与查找）。

## 配置文件

1. 将 [`data/config/config.example.json`](data/config/config.example.json) 复制为根目录下的 **`config.json`**。
2. 在 `config.json` 里填写 **`dashscope_api_key`**（`sk-` 开头），按需修改模型名、分辨率等字段。
3. **`config.json` 已加入 `.gitignore`，请勿提交到 Git。**

查找顺序（未指定 `--config` 时）：**`V2T_CONFIG`** → `./config.json` → `<项目根>/config.json` → `data/config/config.json`。

也可在命令行指定：

```bash
v2t --config /path/to/my-config.json analyze --input ...
```

**优先级**：若同时设置了环境变量与配置文件，**环境变量覆盖配置文件**中的同名字段。

生成相关可选字段（见 `data/config/config.example.json`）：`subject_descriptions`、`reference_urls`、`reference_video_urls`、`reference_video_descriptions`、`max_segment_seconds`。命令行传入的主体会**追加**在配置文件条目之后。

## Web 界面

```bash
v2t-web
# 等价于 python -m video2text.web.app
```

浏览器打开 **http://127.0.0.1:5000**。在「设置」中填写/保存 API Key 与模型；「新建任务」后按步骤操作。生成中断后同一任务可再点「开始/继续生成」复用已下载的片段缓存。

## 用法

```bash
# 仅生成分镜 JSON
v2t analyze --input ./clip.mp4 --output ./storyboard.json --markdown ./storyboard.md

# 按镜头切片多次调用（旧模式）
v2t analyze --input ./clip.mp4 --output ./sb.json --segment-scenes

# 公网视频 URL
v2t analyze --output ./sb.json --video-url 'https://example.com/video.mp4'

# 根据分镜生成并拼接
v2t generate --storyboard ./storyboard.json --output ./out.mp4

# 主体 + 参考图
v2t generate --storyboard ./sb.json --output ./out.mp4 \
  --subject "主角：成年男性，深灰西装" \
  --reference-image ./ref_hero.png

# 一步：分析 + 生成
v2t run --input ./clip.mp4 --output ./out.mp4 --style "水彩插画风格"

# 从主题生成
v2t theme --theme "末班地铁上一把伞" -o ./my_story.json
v2t generate --storyboard ./my_story.json -o ./out.mp4 --text-only-video
v2t run --theme "末班地铁上一把伞" -o ./out.mp4
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

端到端流程需有效 API Key 与配额，请自行用短视频验证。

## 说明与限制

- **默认整片分析**：本地文件若小于 Base64 上限，走 OpenAI 兼容接口；否则自动改用 DashScope 本地上传理解。
- 千问**不分析视频中的音频**；对白依赖口型/字幕推断。
- 万相**文生**单次最长 **15s**；**参考生（r2v）**单次最长 **10s**；长片按上限切段再 ffmpeg 拼接。
