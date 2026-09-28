import asyncio

from oc8.modelrouter.accumulate import StreamTiming, accumulate_stream
from oc8.modelrouter.types import CompletionChunk


async def _chunks():
    yield CompletionChunk(text="")
    yield CompletionChunk(text="hi")


def test_stream_timing_ttft_on_first_text(monkeypatch):
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 0.05
        return clock["t"]

    monkeypatch.setattr("oc8.modelrouter.accumulate.time.monotonic", fake_monotonic)
    probe = StreamTiming()
    result = asyncio.run(accumulate_stream(_chunks(), timing=probe))
    assert result.text == "hi"
    assert probe.ttft_ms is not None and probe.ttft_ms > 0
    assert probe.model_wait_ms >= probe.ttft_ms


def test_stream_timing_absent_is_unchanged():
    result = asyncio.run(accumulate_stream(_chunks()))
    assert result.text == "hi"
