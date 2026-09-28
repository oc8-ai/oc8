import json

from oc8.agent.harness.stages.c_provenance import fence_external
from oc8.agent.provenance import TEXT_PIECE, shorten_long_strings


def test_fence_external_preserves_source_and_output() -> None:
    assert fence_external("customer text", source="things:get_record") == (
        '<external source="things:get_record">\n'
        "customer text\n"
        "</external>"
    )


def test_long_json_strings_come_back_as_short_pieces() -> None:
    long_line = "n" * (TEXT_PIECE + 5)
    body = f"repo: acme/widgets\n{long_line}"
    raw = json.dumps({"description": body, "name": "short"})
    parsed = json.loads(shorten_long_strings(raw))
    pieces = parsed["description"]
    assert pieces[0] == "repo: acme/widgets"
    assert "".join(pieces[1:]) == long_line
    assert all(len(piece) <= TEXT_PIECE for piece in pieces)
    assert parsed["name"] == "short"
    assert shorten_long_strings('{"name": "ok"}') == '{"name": "ok"}'
    plain = "not json at all, " + ("y" * 400)
    assert shorten_long_strings(plain) == plain
