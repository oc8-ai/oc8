from __future__ import annotations

from oc8.supervision.detection import (
    compute_drift_score,
    cosine_similarity,
    loop_detected,
    token_burn_alert,
    tool_error_alert,
    verdict_from_score,
)

_TH = {"steer": 0.35, "rewind": 0.55, "escalate": 0.75}


def test_tier0_watchdogs() -> None:
    assert loop_detected(["a", "a", "a", "b"]) is True  # a repeated 3x
    assert loop_detected(["a", "b", "c", "d"]) is False
    assert loop_detected([]) is False
    assert token_burn_alert(25000, budget=20000) is True
    assert token_burn_alert(100, budget=20000) is False
    assert tool_error_alert(3, 4) is True
    assert tool_error_alert(1, 10) is False


def test_cosine_similarity() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine_similarity([], [1.0]) == 0.0


def test_drift_score_and_verdict() -> None:
    # on-anchor trajectory, no violations -> low score, on_track
    aligned = compute_drift_score(anchor_embedding=[1.0, 0.0], trajectory_embedding=[1.0, 0.0])
    assert aligned == 0.0
    assert verdict_from_score(aligned, _TH) == "on_track"

    # orthogonal trajectory (max divergence) -> high score, off_track
    diverged = compute_drift_score(anchor_embedding=[1.0, 0.0], trajectory_embedding=[0.0, 1.0])
    assert diverged >= 0.55
    assert verdict_from_score(diverged, _TH) == "off_track"

    # no embeddings but constraint violations still raises the score
    viol = compute_drift_score(
        anchor_embedding=None, trajectory_embedding=None, constraint_violations=5
    )
    assert viol > 0.0
    assert verdict_from_score(0.4, _TH) == "drifting"
