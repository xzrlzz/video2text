"""
video2text — 视频分镜与通义万相再生成。

分层架构（依赖自上而下）::

    入口层
        cli        Click 命令行（analyze / theme / generate / run）
        web        Flask API + 静态页（任务与 workspace 断点续传）

    编排层
        pipeline   万相生成编排（与 Web/CLI 共用的切段与参考逻辑）、ffmpeg 拼接

    领域层
        core       分镜模型、视频分析、场景检测、主题创作

    服务层
        services   万相 HTTP、参考媒体预处理

    基础设施
        config     Settings、JSON 配置加载
        utils      项目根目录、workspace/static/data 等路径解析
"""

__all__: list[str] = []
