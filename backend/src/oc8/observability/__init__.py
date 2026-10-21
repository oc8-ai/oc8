"""OpenTelemetry setup — one helper, off by default, fully inert when disabled.

All `opentelemetry.*` imports are lazy (inside functions) so a disabled process
pays nothing. `get_tracer`/`get_meter` return the OTel API's no-op when no
provider is set, so instrumented call sites never need an `if enabled:` guard.
Setup failure is swallowed: telemetry must never take the app down.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from oc8.config import Settings

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter
    from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)

_INSTRUMENTATION_NAME = "oc8"
_providers_set = False


def get_tracer() -> Tracer:
    from opentelemetry import trace

    return trace.get_tracer(_INSTRUMENTATION_NAME)


def get_meter() -> Meter:
    from opentelemetry import metrics

    return metrics.get_meter(_INSTRUMENTATION_NAME)


# Metric instruments are created once per name and cached here. The cache
# holds a reference to whichever meter was active when a name was first
# used, so it MUST be cleared whenever the meter provider changes (see
# shutdown_observability) -- otherwise a later provider swap (e.g. between
# tests, or between "disabled" and "enabled") keeps emitting through a
# stale/no-op instrument forever.
_instruments: dict[str, Any] = {}


def _counter(name: str, unit: str = "1", desc: str = "") -> Any:
    inst = _instruments.get(name)
    if inst is None:
        inst = get_meter().create_counter(name, unit=unit, description=desc)
        _instruments[name] = inst
    return inst


def _histogram(name: str, unit: str, desc: str = "") -> Any:
    inst = _instruments.get(name)
    if inst is None:
        inst = get_meter().create_histogram(name, unit=unit, description=desc)
        _instruments[name] = inst
    return inst


def record_token_usage(*, provider: str, model: str, tokens_in: int, tokens_out: int) -> None:
    """Low-cardinality labels only (provider, model) -- never tenant_id/agent_id/run_id."""
    attrs = {"provider": provider, "model": model}
    _counter("oc8.tokens.in", desc="input tokens consumed").add(tokens_in, attrs)
    _counter("oc8.tokens.out", desc="output tokens produced").add(tokens_out, attrs)


def record_model_latency(*, provider: str, model: str, ms: float) -> None:
    _histogram("oc8.model.latency", "ms", desc="model completion latency").record(
        ms, {"provider": provider, "model": model}
    )


def record_run_outcome(outcome: str) -> None:
    _counter("oc8.runs.total", desc="terminal run outcomes").add(1, {"outcome": outcome})


def record_tool_call(*, tool: str, decision: str) -> None:
    _counter("oc8.tool_calls.total", desc="tool call authorization decisions").add(
        1, {"tool": tool, "decision": decision}
    )


def record_ingestion_job(status: str) -> None:
    _counter("oc8.ingestion.jobs", desc="ingestion job outcomes").add(1, {"status": status})


def record_budget_exceeded(kind: str) -> None:
    _counter("oc8.budget.exceeded", desc="budget threshold breaches").add(1, {"kind": kind})


def _http_signal_url(settings: Settings, signal: str) -> str:
    """OTLP/HTTP takes a full per-signal URL: base endpoint + /v1/<signal>."""
    return f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/{signal}"


def _build_span_exporter(settings: Settings) -> Any:
    if settings.otel_exporter == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()
    if settings.otel_exporter == "none":
        return None
    if settings.otel_exporter == "otlp_http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpSpanExporter,
        )

        return HttpSpanExporter(endpoint=_http_signal_url(settings, "traces"))
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)


def _build_metric_reader(settings: Settings) -> Any:
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )

    if settings.otel_exporter == "console":
        return PeriodicExportingMetricReader(ConsoleMetricExporter())
    if settings.otel_exporter == "none":
        return None
    if settings.otel_exporter == "otlp_http":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter as HttpMetricExporter,
        )

        return PeriodicExportingMetricReader(
            HttpMetricExporter(endpoint=_http_signal_url(settings, "metrics"))
        )
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    return PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    )


def setup_observability(settings: Settings) -> None:
    """Idempotent. Does nothing when otel_enabled is False."""
    global _providers_set
    if not settings.otel_enabled or _providers_set:
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
        tracer_provider = TracerProvider(resource=resource)
        span_exporter = _build_span_exporter(settings) if settings.otel_traces_enabled else None
        if span_exporter is not None:
            tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

        if settings.otel_metrics_enabled:
            from opentelemetry.sdk.metrics import MeterProvider

            reader = _build_metric_reader(settings)
            meter_provider = MeterProvider(
                resource=resource, metric_readers=[reader] if reader is not None else []
            )
            metrics.set_meter_provider(meter_provider)

        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        HTTPXClientInstrumentor().instrument()
        LoggingInstrumentor().instrument(set_logging_format=True)

        _providers_set = True
        logger.info("observability enabled (exporter=%s)", settings.otel_exporter)
    except Exception as exc:
        logger.warning("observability setup failed, continuing disabled: %s", exc)


def shutdown_observability() -> None:
    global _providers_set
    # Always drop cached instruments, even when disabled: a call site may have
    # created a (no-op) instrument before setup ever ran, or a test may swap
    # in its own provider without going through setup_observability at all.
    # Either way, a stale cached instrument must not survive a provider swap.
    _instruments.clear()
    if not _providers_set:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception as exc:
        logger.warning("observability shutdown failed: %s", exc)
    finally:
        _providers_set = False
