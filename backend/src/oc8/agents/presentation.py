"""Display-only fields on `agent.presentation` that mirror real configuration.

`presentation.llm` / `presentation.provider` are what the agent list, cost
estimates and model-based budget targeting show. They are NOT versioned (they
are presentation, not behaviour), so every writer of `agent.model_config_id` --
`switch_model`, the copilot's model switch, and rollback -- has to refresh them
itself, or the UI keeps naming a model the agent no longer runs.
"""

from __future__ import annotations

from oc8 import models as m


def show_model(agent: m.Agent, mc: m.ModelConfig | None) -> None:
    """Point the displayed model name/provider at `mc`; `None` clears them.

    jsonb: replaced whole, or SQLAlchemy never notices the mutation.
    """
    presentation = dict(agent.presentation or {})
    if mc is None:
        presentation.pop("llm", None)
        presentation.pop("provider", None)
    else:
        presentation["llm"] = mc.display_name or mc.model
        presentation["provider"] = mc.provider
    agent.presentation = presentation
