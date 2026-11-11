"""Which models can think: one rule for built-in providers, and a capa's own
declaration for its provider (spec §4, §6)."""

from __future__ import annotations

from typing import Any

import pytest

from oc8.capas.contributions import PluginContributions
from oc8.modelrouter import registry
from oc8.modelrouter.reasoning_support import (
    anthropic_thinks,
    ollama_has_think_levels,
    ollama_thinks,
    openai_compatible_thinks,
    openai_thinks,
)


@pytest.mark.parametrize(
    ("model", "thinks"),
    [
        ("claude-sonnet-5", True),
        ("claude-opus-4-1-20250805", True),
        ("claude-haiku-4-5", True),
        ("claude-3-7-sonnet-20250219", True),
        ("claude-3-5-sonnet-20241022", False),
        ("claude-3-haiku", False),
        ("gpt-4o", False),
        ("gpt-5-chat-latest", False),
        ("o3d", False),
        ("gpt-oss", False),
    ],
)
def test_anthropic(model: str, thinks: bool) -> None:
    assert anthropic_thinks(model) is thinks


@pytest.mark.parametrize(
    ("model", "thinks"),
    [
        ("o3-mini", True),
        ("o1", True),
        ("o4-mini:high", True),
        ("gpt-5.1-codex", True),
        ("gpt-4o", False),
        ("omni-moderation-latest", False),
        ("o10x", False),
        ("o3d", False),
        ("gpt-5-chat-latest", False),
    ],
)
def test_openai(model: str, thinks: bool) -> None:
    assert openai_thinks(model) is thinks


@pytest.mark.parametrize(
    ("model", "thinks"),
    [
        ("openai/o3-mini", True),
        ("deepseek/deepseek-r1", True),
        ("magistral-medium", True),
        ("anthropic/claude-sonnet-4.5", True),
        ("mistral-large-latest", False),
    ],
)
def test_openai_compatible_strips_the_vendor_prefix(model: str, thinks: bool) -> None:
    assert openai_compatible_thinks(model) is thinks


@pytest.mark.parametrize(
    ("model", "thinks", "levels"),
    [
        ("qwen3:8b", True, False),
        ("deepseek-r1:7b", True, False),
        ("gpt-oss:20b", True, True),
        ("magistral:24b", True, False),
        ("llama3.1:8b", False, False),
        ("hf.co/unsloth/Qwen3-8B-GGUF", True, False),
    ],
)
def test_ollama(model: str, thinks: bool, levels: bool) -> None:
    assert ollama_thinks(model) is thinks
    assert ollama_has_think_levels(model) is levels


@pytest.mark.parametrize(
    ("provider", "model", "thinks"),
    [
        ("anthropic", "claude-sonnet-5", True),
        ("claude", "claude-sonnet-5", True),
        ("openai_chatgpt", "anything", True),
        ("Ollama", "qwen3:8b", True),
        ("ollama", "llama3.1:8b", False),
        ("auto", "router", False),
        ("nope", "x", False),
    ],
)
def test_supports_reasoning_for_reads_the_built_in_declarations(
    provider: str, model: str, thinks: bool
) -> None:
    assert registry.supports_reasoning_for(provider, model) is thinks


def _entry(declared: Any) -> registry.ProviderEntry:
    contrib = PluginContributions("demo_provider")
    contrib.add_model_provider(
        canonical="demo-llm",
        locality="cloud",
        factory=lambda _s, _b, _k: None,
        available=lambda _s, _k: True,
        supports_reasoning=declared,
    )
    return contrib.providers["demo-llm"]


def test_a_capa_declares_per_model() -> None:
    entry = _entry(lambda model: model.startswith("demo-think"))
    assert registry.entry_supports_reasoning(entry, "demo-think-1") is True
    assert registry.entry_supports_reasoning(entry, "demo-1") is False


def test_a_capa_that_declares_nothing_cannot_think() -> None:
    contrib = PluginContributions("demo_provider")
    contrib.add_model_provider(
        canonical="demo-llm",
        locality="cloud",
        factory=lambda _s, _b, _k: None,
        available=lambda _s, _k: True,
    )
    assert registry.entry_supports_reasoning(contrib.providers["demo-llm"], "x") is False


def test_a_raising_declaration_reads_as_cannot_think() -> None:
    def boom(model: str) -> bool:
        raise RuntimeError("capa bug")

    assert registry.entry_supports_reasoning(_entry(boom), "demo-think-1") is False


def test_a_declaration_that_is_neither_bool_nor_callable_is_refused() -> None:
    with pytest.raises(ValueError, match="supports_reasoning"):
        _entry("yes")


def test_supports_reasoning_for_sees_a_loaded_capa(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _entry(lambda model: model == "demo-think-1")
    monkeypatch.setattr(registry, "_plugin_entries", lambda: {"demo-llm": entry})
    assert registry.supports_reasoning_for("demo-llm", "demo-think-1") is True
    assert registry.supports_reasoning_for("demo-llm", "demo-1") is False
