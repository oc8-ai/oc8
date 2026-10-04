from oc8.copilot.door import door_of


def test_door_of() -> None:
    assert door_of({"door": "followup", "chat_channel": "telegram"}) == "followup"
    assert door_of({"chat_channel": "telegram", "chat_channel_external_id": "1"}) == "telegram"
    assert door_of({"door": "web"}) == "web"
    assert door_of(None) == "web"
