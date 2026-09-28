import time

from oc8.agent.harness.step_timing import (
    finish_step,
    latency_lines,
    note_model,
    note_tools,
    percentile,
    start_step,
)


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([10, 20, 30, 40], 50) == 20
    assert percentile([10, 20, 30, 40], 95) == 40


def test_latency_lines_pool_and_small_sample():
    records = [
        {"step": 1, "model_wait_ms": 100, "ttft_ms": 40, "tool_wait_ms": 50, "step_wall_ms": 160},
        {"step": 2, "model_wait_ms": 300, "ttft_ms": None, "tool_wait_ms": 0, "step_wall_ms": 300},
    ]
    text = "\n".join(latency_lines(records))
    assert "p50 step_wall_ms: 160" in text
    assert "p95 step_wall_ms: n/a (n<8)" in text
    assert "p50 ttft_ms: 40" in text
    assert "ttft samples: 1" in text
    assert "p50 ttft_ms step 1: 40" in text
    assert "p50 ttft_ms step>1: n/a" in text
    assert latency_lines([]) == ["No step timings."]


def test_finish_step_drops_clock_and_covers_waits():
    rec = start_step(3)
    note_model(rec, model_wait_ms=100, ttft_ms=40)
    note_tools(rec, 25)
    time.sleep(0.01)
    done = finish_step(rec)
    assert set(done) == {"step", "model_wait_ms", "ttft_ms", "tool_wait_ms", "step_wall_ms"}
    assert done["step"] == 3
    assert done["step_wall_ms"] >= 100
    assert "_t0" not in done
