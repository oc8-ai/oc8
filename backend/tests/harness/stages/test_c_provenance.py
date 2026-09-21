from oc8.agent.harness.stages.c_provenance import fence_external


def test_fence_external_preserves_source_and_output() -> None:
    assert fence_external("customer text", source="things:get_record") == (
        '<external source="things:get_record">\n'
        "customer text\n"
        "</external>"
    )
