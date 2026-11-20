from __future__ import annotations

from pathlib import Path

from oc8_evals.mocks._store import Store


def test_absent_file_reads_as_empty(tmp_path: Path) -> None:
    assert Store(tmp_path / "s.json").read() == {}


def test_update_is_read_modify_write_and_returns_the_callbacks_value(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.json")

    def add(state: dict[str, object]) -> str:
        msgs = state.setdefault("messages", [])
        assert isinstance(msgs, list)
        msgs.append({"id": store.next_id(state, "msg"), "subject": "hi"})
        return "added"

    assert store.update(add) == "added"
    assert store.update(add) == "added"
    state = store.read()
    assert [m["id"] for m in state["messages"]] == ["msg-1", "msg-2"]


def test_next_id_is_monotonic_per_prefix(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.json")
    state: dict[str, object] = {}
    assert store.next_id(state, "evt") == "evt-1"
    assert store.next_id(state, "evt") == "evt-2"
    assert store.next_id(state, "msg") == "msg-1"
