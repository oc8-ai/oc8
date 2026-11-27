# Models

**Route:** `/models` · **Settings** section

## What it is

Where you register **LLM providers** and model configs agents can use
(Anthropic, OpenAI, Ollama, …), including pricing metadata for [Costs](costs.md).

## What it is for

- Register LLM providers and model configs agents can use
- Usually done **once** — often already handled in the [Welcome wizard](welcome.md)
- Adjust later for cost ([Costs](costs.md)) or different agents

You only need this screen before an agent **runs** — not to install oc8 itself.

## Where you are in the flow

```text
★ Models configured → assign on Agents → runs consume tokens → Costs
```

Without a model, agents cannot usefully run. This is early setup (also linked
from the welcome / pilot checklist).

## What you do here

1. Add a provider (API key via env or UI, depending on deployment).
2. Declare / select models.
3. On each [Agent](agents.md), pick the model config.
4. Prefer cheaper models for pilots; tighten later.

## Speech models

A model of an OpenAI or OpenAI-compatible provider can have the **Capability**
**Speech to text** (dictation) or **Text to speech** (reading answers aloud)
instead of **Chat**. A speech model is never offered for chats, agents, the
Copilot or the auto tiers; the list shows its capability where a chat model
has its chat switch. Pick it under Settings → **Speech** (see
[Settings](settings-general.md#speech)). Reading an answer with restricted
content aloud needs a read-aloud model whose locality is local.

## Where work goes next

| Goal | Next |
|------|------|
| Run an agent | [Agents](agents.md) |
| Local model without cloud keys | Ollama profile — [Quickstart](../quickstart.md) |
| Watch spend | [Costs](costs.md) |

## Related

- [Credentials](credentials.md) (secrets for tools — different from model keys)
- [Quickstart](../quickstart.md)
