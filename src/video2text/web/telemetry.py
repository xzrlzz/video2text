"""Telemetry helpers for logging, metrics, and tracing."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, g, request, session

_request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
_task_id_ctx: ContextVar[str | None] = ContextVar("task_id", default=None)
_user_ctx: ContextVar[str | None] = ContextVar("user", default=None)

_INIT_LOCK = threading.Lock()
_INITIALIZED = False

_LOG = logging.getLogger(__name__)


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def get_request_id() -> str | None:
    return _request_id_ctx.get()


def get_task_id() -> str | None:
    return _task_id_ctx.get()


def get_current_user() -> str | None:
    return _user_ctx.get()


@contextmanager
def bind_log_context(
    *,
    request_id: str | None = None,
    task_id: str | None = None,
    user: str | None = None,
):
    tokens: list[tuple[ContextVar[str | None], Any]] = []
    try:
        if request_id is not None:
            tokens.append((_request_id_ctx, _request_id_ctx.set(request_id)))
        if task_id is not None:
            tokens.append((_task_id_ctx, _task_id_ctx.set(task_id)))
        if user is not None:
            tokens.append((_user_ctx, _user_ctx.set(user)))
        yield
    finally:
        for ctx_var, token in reversed(tokens):
            ctx_var.reset(token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = _request_id_ctx.get()
        if not getattr(record, "task_id", None):
            record.task_id = _task_id_ctx.get()
        if not getattr(record, "user", None):
            record.user = _user_ctx.get()
        return True


class _JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service_name,
            "environment": self._environment,
        }
        for key in (
            "event",
            "request_id",
            "task_id",
            "user",
            "route",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "task_type",
            "task_status",
            "owner",
            "client_ip",
        ):
            v = getattr(record, key, None)
            if v is not None and v != "":
                payload[key] = v
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("_"):
                continue
            if key in payload:
                continue
            if key in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "message",
            }:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        service_name = os.getenv("V2T_SERVICE_NAME", "video2text-web").strip() or "video2text-web"
        environment = os.getenv("V2T_ENV", "prod").strip() or "prod"
        level_name = os.getenv("V2T_LOG_LEVEL", "INFO").strip().upper() or "INFO"
        level = getattr(logging, level_name, logging.INFO)

        root = logging.getLogger()
        root.setLevel(level)
        root.handlers.clear()

        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(_JsonFormatter(service_name=service_name, environment=environment))
        handler.addFilter(_ContextFilter())
        root.addHandler(handler)

        logging.captureWarnings(True)
        _INITIALIZED = True
        _LOG.info(
            "logging configured",
            extra={"event": "logging_init", "status_code": 0},
        )


@dataclass
class _MetricsStore:
    http_requests: dict[tuple[str, str, str], int]
    http_duration_sum: dict[tuple[str, str], float]
    http_duration_count: dict[tuple[str, str], int]
    task_events: dict[tuple[str, str], int]
    exceptions: dict[str, int]
    lock: threading.Lock


_METRICS = _MetricsStore(
    http_requests={},
    http_duration_sum={},
    http_duration_count={},
    task_events={},
    exceptions={},
    lock=threading.Lock(),
)


def record_http_request(method: str, route: str, status_code: int, duration_ms: float) -> None:
    m = method.upper()
    r = route or "unknown"
    s = str(status_code)
    key3 = (m, r, s)
    key2 = (m, r)
    with _METRICS.lock:
        _METRICS.http_requests[key3] = _METRICS.http_requests.get(key3, 0) + 1
        _METRICS.http_duration_sum[key2] = _METRICS.http_duration_sum.get(key2, 0.0) + (duration_ms / 1000.0)
        _METRICS.http_duration_count[key2] = _METRICS.http_duration_count.get(key2, 0) + 1


def record_task_event(task_type: str, task_status: str) -> None:
    t = (task_type or "unknown").strip() or "unknown"
    s = (task_status or "unknown").strip() or "unknown"
    with _METRICS.lock:
        key = (t, s)
        _METRICS.task_events[key] = _METRICS.task_events.get(key, 0) + 1


def record_exception(kind: str) -> None:
    k = (kind or "unknown").strip() or "unknown"
    with _METRICS.lock:
        _METRICS.exceptions[k] = _METRICS.exceptions.get(k, 0) + 1


def render_prometheus_metrics() -> str:
    lines: list[str] = []
    lines.append("# HELP v2t_http_requests_total Total HTTP requests.")
    lines.append("# TYPE v2t_http_requests_total counter")
    with _METRICS.lock:
        for (method, route, status), count in sorted(_METRICS.http_requests.items()):
            lines.append(
                f'v2t_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
            )

        lines.append("# HELP v2t_http_request_duration_seconds_sum Sum of HTTP request duration.")
        lines.append("# TYPE v2t_http_request_duration_seconds_sum counter")
        for (method, route), total in sorted(_METRICS.http_duration_sum.items()):
            lines.append(
                f'v2t_http_request_duration_seconds_sum{{method="{method}",route="{route}"}} {total:.6f}'
            )

        lines.append("# HELP v2t_http_request_duration_seconds_count Count of HTTP requests for duration.")
        lines.append("# TYPE v2t_http_request_duration_seconds_count counter")
        for (method, route), count in sorted(_METRICS.http_duration_count.items()):
            lines.append(
                f'v2t_http_request_duration_seconds_count{{method="{method}",route="{route}"}} {count}'
            )

        lines.append("# HELP v2t_task_events_total Total task lifecycle events.")
        lines.append("# TYPE v2t_task_events_total counter")
        for (task_type, status), count in sorted(_METRICS.task_events.items()):
            lines.append(
                f'v2t_task_events_total{{task_type="{task_type}",status="{status}"}} {count}'
            )

        lines.append("# HELP v2t_exceptions_total Total exceptions recorded by kind.")
        lines.append("# TYPE v2t_exceptions_total counter")
        for kind, count in sorted(_METRICS.exceptions.items()):
            lines.append(f'v2t_exceptions_total{{kind="{kind}"}} {count}')
    lines.append("")
    return "\n".join(lines)


def _setup_optional_otel(app: Flask) -> None:
    if not _truthy(os.getenv("V2T_OTEL_ENABLED")):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        _LOG.warning(
            "otel enabled but dependencies missing",
            extra={"event": "otel_init_skipped"},
        )
        return

    service_name = os.getenv("V2T_SERVICE_NAME", "video2text-web").strip() or "video2text-web"
    environment = os.getenv("V2T_ENV", "prod").strip() or "prod"
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()

    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": environment,
        }
    )
    provider = TracerProvider(resource=resource)
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    FlaskInstrumentor().instrument_app(app)
    _LOG.info(
        "otel tracing initialized",
        extra={"event": "otel_init", "path": endpoint or "default"},
    )


def init_observability(app: Flask) -> None:
    configure_logging()
    _setup_optional_otel(app)
    access_log = logging.getLogger("video2text.access")

    @app.before_request
    def _before_request_logging() -> None:
        rid = (request.headers.get("X-Request-ID") or "").strip() or uuid.uuid4().hex
        g.request_id = rid
        g.started_at = time.perf_counter()
        _request_id_ctx.set(rid)

        user = session.get("user")
        if isinstance(user, str) and user:
            _user_ctx.set(user)
        else:
            _user_ctx.set(None)

    @app.after_request
    def _after_request_logging(response):
        rid = getattr(g, "request_id", None) or _request_id_ctx.get()
        if rid:
            response.headers["X-Request-ID"] = rid
        started = float(getattr(g, "started_at", time.perf_counter()))
        duration_ms = (time.perf_counter() - started) * 1000.0
        route = request.url_rule.rule if request.url_rule else request.path
        record_http_request(request.method, route, response.status_code, duration_ms)
        access_log.info(
            "http request",
            extra={
                "event": "http_request",
                "request_id": rid,
                "route": route,
                "path": request.path,
                "method": request.method,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "client_ip": request.remote_addr or "",
            },
        )
        return response

    @app.teardown_request
    def _teardown_request_logging(exc):
        if exc is not None:
            record_exception("flask_teardown")
            _LOG.exception(
                "request teardown exception",
                extra={"event": "request_teardown_exception"},
            )
        _request_id_ctx.set(None)
        _user_ctx.set(None)


def metrics_response() -> Response:
    return Response(
        render_prometheus_metrics(),
        mimetype="text/plain; version=0.0.4; charset=utf-8",
    )

