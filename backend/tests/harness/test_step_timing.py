from oc8.agent.harness.step_timing import latency_lines, percentile


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
    assert latency_lines([]) == ["No step timings."]
