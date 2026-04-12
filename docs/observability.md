# 观测与日志落地说明

本文说明当前 `video2text` 的观测能力、哪些是必须项、以及如何接入集中平台。

## 1. 当前已具备能力

- 应用日志为结构化 JSON（stdout）。
- 每个请求自动生成/透传 `X-Request-ID`。
- 任务链路包含 `task_id` / `task_status` / `task_type` 字段。
- 暴露探活与指标端点：
  - `GET /health`
  - `GET /metrics`（Prometheus 文本格式）
- 可选 OTel Trace 初始化（开启开关后生效）。

## 2. 必须项与可选项

### 必须项（建议生产都做）

1. 使用结构化日志（当前已实现）。
2. 把 stdout 日志采集到集中平台（ELK / Loki / 云日志）。
3. 定期抓取 `/metrics`。

### 可选项（建议逐步开启）

1. OTel Trace（需要额外依赖与 Collector）。
2. OTel Metrics/Logs 直传（按平台能力决定）。
3. 告警规则与 Dashboard（5xx、任务失败率、外部调用耗时）。

## 3. 依赖安装

基础运行依赖：

```bash
pip install -r requirements.txt
```

可观测增强依赖（OTel）：

```bash
pip install -r requirements-observability.txt
```

## 4. 运行时环境变量

不开启 OTel 也能跑（默认）：

```bash
V2T_LOG_LEVEL=INFO
V2T_SERVICE_NAME=video2text-web
V2T_ENV=prod
```

开启 OTel Trace（可选）：

```bash
V2T_OTEL_ENABLED=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

## 5. Collector 示例

仓库提供模板：

- `deploy/otel/collector.config.example.yaml`

启动示例（机器已安装 `otelcol-contrib`）：

```bash
otelcol-contrib --config deploy/otel/collector.config.example.yaml
```

该模板默认包含：

- OTLP traces 接收与转发
- Prometheus 抓取 `video2text` 的 `/metrics`
- 可选 filelog/logs pipeline（按你的日志后端启用）

## 6. 推荐接入顺序

1. 先把 stdout JSON 日志接到集中平台。
2. 再接 Prometheus 抓取 `/metrics`。
3. 最后启用 OTel Trace，并在平台按 `request_id` / `task_id` 建检索面板。
