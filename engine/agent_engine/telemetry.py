from __future__ import annotations

import os
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Iterator

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
_token_usage_total: ContextVar[int] = ContextVar("token_usage_total", default=0)
_token_usage_baseline: ContextVar[int] = ContextVar("token_usage_baseline", default=0)
_configured = False

try:
    from opentelemetry import metrics, propagate, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.trace import SpanKind, Status, StatusCode
except Exception:  # pragma: no cover - lets compile/run in bare environments.
    metrics = None
    propagate = None
    trace = None
    MeterProvider = None
    PeriodicExportingMetricReader = None
    ConsoleMetricExporter = None
    Resource = None
    TracerProvider = None
    BatchSpanProcessor = None
    ConsoleSpanExporter = None
    SpanKind = None
    Status = None
    StatusCode = None


class _NoopInstrument:
    def add(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _MetricSet:
    def __init__(self) -> None:
        meter = metrics.get_meter("hethongagent.engine") if metrics else None
        if meter:
            self.run_latency = meter.create_histogram("agent.run.latency.ms", unit="ms", description="End-to-end task run latency.")
            self.queue_latency = meter.create_histogram("agent.queue.latency.ms", unit="ms", description="Broker queued-to-start latency.")
            self.runs = meter.create_counter("agent.runs.total", description="Agent runs by status.")
            self.verifications = meter.create_counter("agent.verifications.total", description="Verification outcomes.")
            self.reworks = meter.create_counter("agent.reworks.total", description="Rework loop attempts.")
            self.token_cost = meter.create_counter("agent.token_cost.tokens", unit="tokens", description="LLM token usage as cost proxy.")
            self.sandbox_failures = meter.create_counter("agent.sandbox_failures.total", description="Sandbox creation/execution failures.")
            self.approval_latency = meter.create_histogram("agent.approval.latency.ms", unit="ms", description="Human approval latency.")
            self.crash_recoveries = meter.create_counter("agent.crash_recoveries.total", description="Recovered incomplete broker runs.")
            self.broker_messages = meter.create_counter("agent.broker.messages.total", description="Broker messages/events emitted.")
        else:
            self.run_latency = _NoopInstrument()
            self.queue_latency = _NoopInstrument()
            self.runs = _NoopInstrument()
            self.verifications = _NoopInstrument()
            self.reworks = _NoopInstrument()
            self.token_cost = _NoopInstrument()
            self.sandbox_failures = _NoopInstrument()
            self.approval_latency = _NoopInstrument()
            self.crash_recoveries = _NoopInstrument()
            self.broker_messages = _NoopInstrument()


METRICS = _MetricSet()


def configure_telemetry(service_name: str = "hethongagent-engine") -> None:
    global _configured, METRICS
    if _configured or not trace or not metrics:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "0.1.0",
            "deployment.environment": os.getenv("OTEL_ENVIRONMENT", "local"),
        }
    )

    trace_provider = TracerProvider(resource=resource)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except Exception:
            pass
    elif os.getenv("OTEL_CONSOLE_EXPORTER", "").lower() in {"1", "true", "yes"}:
        trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    trace.set_tracer_provider(trace_provider)

    metric_readers = []
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            metric_readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
        except Exception:
            pass
    elif os.getenv("OTEL_CONSOLE_EXPORTER", "").lower() in {"1", "true", "yes"}:
        metric_readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter(out=sys.stderr)))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=metric_readers))
    METRICS = _MetricSet()
    _configured = True


def tracer():
    configure_telemetry()
    return trace.get_tracer("hethongagent.engine") if trace else None


def meter_metrics() -> _MetricSet:
    configure_telemetry()
    return METRICS


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def set_correlation_id(value: str | None = None) -> str:
    cid = str(value or "").strip() or new_correlation_id()
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    cid = _correlation_id.get()
    if cid:
        return cid
    return set_correlation_id()


def reset_token_usage() -> None:
    _token_usage_total.set(0)
    _token_usage_baseline.set(0)


def get_token_usage() -> int:
    return _token_usage_total.get()


def get_token_usage_delta() -> dict[str, int]:
    """Snapshot the per-node token delta. Stores baseline on the contextvar so
    the next call returns tokens recorded since this call. Safe to invoke from
    any node lifecycle hook; returns {} if no usage recorded since last reset.
    """
    total = _token_usage_total.get()
    baseline = _token_usage_baseline.get()
    delta = max(0, int(total) - int(baseline))
    _token_usage_baseline.set(int(total))
    if delta <= 0:
        return {}
    return {"total": delta}


def inject_trace_context(carrier: dict[str, str]) -> dict[str, str]:
    if propagate:
        propagate.inject(carrier)
    carrier["x-correlation-id"] = get_correlation_id()
    return carrier


def extract_trace_context(carrier: dict[str, str]):
    if propagate:
        return propagate.extract(carrier)
    return None


@contextmanager
def start_span(name: str, attributes: dict[str, Any] | None = None, kind: Any | None = None, context: Any | None = None) -> Iterator[Any]:
    active_tracer = tracer()
    attrs = dict(attributes or {})
    attrs.setdefault("correlation.id", get_correlation_id())
    if not active_tracer:
        yield None
        return
    span_kind = kind if kind is not None else (SpanKind.INTERNAL if SpanKind else None)
    kwargs: dict[str, Any] = {"attributes": attrs}
    if span_kind is not None:
        kwargs["kind"] = span_kind
    if context is not None:
        kwargs["context"] = context
    with active_tracer.start_as_current_span(name, **kwargs) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            if Status and StatusCode:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def set_span_attrs(span: Any, attributes: dict[str, Any]) -> None:
    if not span:
        return
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def now_ms() -> float:
    return time.perf_counter() * 1000


def elapsed_ms(start_ms: float) -> float:
    return max(0.0, now_ms() - start_ms)


def parse_iso_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp() * 1000
    except Exception:
        return None


def record_run_latency(duration_ms: float, status: str) -> None:
    attrs = {"status": status, "correlation.id": get_correlation_id()}
    metric_set = meter_metrics()
    metric_set.run_latency.record(duration_ms, attrs)
    metric_set.runs.add(1, attrs)


def record_queue_latency(duration_ms: float, role: str) -> None:
    meter_metrics().queue_latency.record(duration_ms, {"role": role, "correlation.id": get_correlation_id()})


def record_verification(passed: bool) -> None:
    meter_metrics().verifications.add(1, {"status": "passed" if passed else "failed", "correlation.id": get_correlation_id()})


def record_rework(count: int = 1) -> None:
    meter_metrics().reworks.add(count, {"correlation.id": get_correlation_id()})


def record_token_usage(total_tokens: int, model: str) -> None:
    if total_tokens > 0:
        _token_usage_total.set(_token_usage_total.get() + total_tokens)
        meter_metrics().token_cost.add(total_tokens, {"model": model, "correlation.id": get_correlation_id()})


def record_sandbox_failure(reason: str) -> None:
    meter_metrics().sandbox_failures.add(1, {"reason": reason[:80], "correlation.id": get_correlation_id()})


def record_approval_latency(duration_ms: float, risk_class: str) -> None:
    meter_metrics().approval_latency.record(duration_ms, {"risk.class": risk_class, "correlation.id": get_correlation_id()})


def record_crash_recoveries(count: int) -> None:
    if count > 0:
        meter_metrics().crash_recoveries.add(count, {"correlation.id": get_correlation_id()})


def record_broker_message(event_type: str, role: str | None) -> None:
    meter_metrics().broker_messages.add(1, {"event.type": event_type, "role": role or "", "correlation.id": get_correlation_id()})
