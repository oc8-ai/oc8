"""State-diff evaluation suite for the agent harness (spec §9).

Lives outside `src/` and outside `tests/` on purpose: it drives REAL runs on
the dev stack and reads the target systems back; the normal `pytest` never
collects it. Its own deterministic tests are under `evals/tests/`.
"""
