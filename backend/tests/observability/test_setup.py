from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from oc8.config import Settings
from oc8.observability import (
    get_meter,
    get_tracer,
    setup_observability,
    shutdown_observability,
)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    shutdown_observability()
    # The OTel API's set_tracer_provider/set_meter_provider are "first write
    # wins" (a `Once` guard) -- once a real SDK provider is installed by one
    # test, later tests calling set_tracer_provider/set_meter_provider again
    # are silently ignored. shutdown_observability() only shuts down
    # processors/readers, it doesn't (and in production shouldn't) unset the
    # global provider. Reset the private globals here so each test in this
    # module gets a clean proxy provider, mirroring how OTel's own test suite
    # (opentelemetry.test.globals_test) resets between tests.
    from opentelemetry import metrics, trace
    from opentelemetry.util._once import Once

    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    metrics._METER_PROVIDER = None  # type: ignore[attr-defined]
    metrics._METER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]


def test_disabled_installs_no_provider() -> None:
    from opentelemetry import trace

    before = type(trace.get_tracer_provider()).__name__
    setup_observability(Settings(otel_enabled=False))
    after = type(trace.get_tracer_provider()).__name__
    # No real provider was set — still the API's default/proxy provider.
    assert after == before
    assert "SDK" not in after and "TracerProvider" not in after.replace("ProxyTracerProvider", "")


def test_get_tracer_is_usable_when_disabled() -> None:
    # An instrumented call site must run with no provider and simply record nothing.
    tracer = get_tracer()
    with tracer.start_as_current_span("x") as span:
        span.set_attribute("k", "v")  # no error, no-op


def test_get_meter_is_usable_when_disabled() -> None:
    meter = get_meter()
    counter = meter.create_counter("oc8.test.counter")
    counter.add(1, {"label": "v"})  # no error, no-op


def test_enabled_with_none_exporter_sets_a_real_tracer_provider() -> None:
    from opentelemetry import trace

    setup_observability(Settings(otel_enabled=True, otel_exporter="none"))
    provider = trace.get_tracer_provider()
    assert "TracerProvider" in type(provider).__name__
    assert "Proxy" not in type(provider).__name__


def test_records_a_span_with_in_memory_exporter() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # Drive the API directly against an in-memory provider to prove the get_tracer
    # call sites emit correctly, without any exporter I/O.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from opentelemetry import trace

    trace.set_tracer_provider(provider)
    try:
        with get_tracer().start_as_current_span("run.execute") as span:
            span.set_attribute("run_id", "abc")
        spans = exporter.get_finished_spans()
        assert [s.name for s in spans] == ["run.execute"]
        attributes = spans[0].attributes
        assert attributes is not None
        assert attributes["run_id"] == "abc"
    finally:
        provider.shutdown()


def test_setup_swallows_exporter_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A broken exporter must never take the process down.
    import oc8.observability as obs

    def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("exporter is broken")

    monkeypatch.setattr(obs, "_build_span_exporter", _boom)
    # Alembic's fileConfig (run once per session by the `settings_env` fixture,
    # via alembic.ini's disable_existing_loggers default of True) disables any
    # logger that already exists at that point -- including this one, created
    # at collection time by this module's top-level `from oc8.observability
    # import ...`. Undo that so caplog can actually observe records from it
    # (same root cause noted in tests/skills/test_runtime.py).
    logging.getLogger("oc8.observability").disabled = False
    with caplog.at_level(logging.WARNING, logger="oc8.observability"):
        setup_observability(
            Settings(
                otel_enabled=True, otel_exporter="otlp", otel_exporter_otlp_endpoint="http://x:4317"
            )
        )
    assert any(
        "observability" in r.message.lower() or "otel" in r.message.lower() for r in caplog.records
    )


def test_validator_requires_endpoint_for_otlp() -> None:
    with pytest.raises(ValueError, match="otel_exporter_otlp_endpoint"):
        Settings(otel_enabled=True, otel_exporter="otlp", otel_exporter_otlp_endpoint="")


def test_disabled_suite_unaffected() -> None:
    # The default Settings must be otel-off.
    assert Settings().otel_enabled is False


def test_validator_requires_endpoint_for_otlp_http() -> None:
    with pytest.raises(ValueError, match="otel_exporter_otlp_endpoint"):
        Settings(otel_enabled=True, otel_exporter="otlp_http", otel_exporter_otlp_endpoint="")


def test_traces_enabled_by_default() -> None:
    assert Settings().otel_traces_enabled is True


def test_otlp_http_builds_http_exporters_with_signal_paths() -> None:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    import oc8.observability as obs

    s = Settings(
        otel_enabled=True,
        otel_exporter="otlp_http",
        otel_exporter_otlp_endpoint="http://collector:4318/",
    )
    span_exporter = obs._build_span_exporter(s)
    assert isinstance(span_exporter, OTLPSpanExporter)
    assert span_exporter._endpoint == "http://collector:4318/v1/traces"
    reader = obs._build_metric_reader(s)
    assert isinstance(reader._exporter, OTLPMetricExporter)
    assert reader._exporter._endpoint == "http://collector:4318/v1/metrics"


def test_traces_disabled_attaches_no_span_processor_but_metrics_flow() -> None:
    from opentelemetry import metrics, trace

    setup_observability(
        Settings(
            otel_enabled=True,
            otel_exporter="console",
            otel_traces_enabled=False,
            otel_metrics_enabled=True,
        )
    )
    tracer_provider = trace.get_tracer_provider()
    assert not tracer_provider._active_span_processor._span_processors  # type: ignore[attr-defined]
    meter_provider = metrics.get_meter_provider()
    readers = meter_provider._all_metric_readers  # type: ignore[attr-defined]
    assert any(type(r).__name__ == "PeriodicExportingMetricReader" for r in readers)


def test_traces_disabled_never_builds_span_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    import oc8.observability as obs

    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("span exporter must not be built")

    monkeypatch.setattr(obs, "_build_span_exporter", _boom)
    setup_observability(
        Settings(otel_enabled=True, otel_exporter="console", otel_traces_enabled=False)
    )
    assert obs._providers_set is True
