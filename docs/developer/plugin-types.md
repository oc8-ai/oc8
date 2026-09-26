# Capa Types

Every capa declares exactly one `type` in `plugin.toml`. This page lists
what each one does, whether it works today, and whether it needs Python
code.

| `type` | What it contributes | Needs code? |
|---|---|---|
| `department_template` | A department + its team of agents, instantiated on demand | no |
| `agent_template` | A single agent, instantiated on demand | no |
| `skill` | A reusable Skill, added to the library on enable | no |
| `flow_template` | A Flow + version, added on enable | no |
| `tool_pack` | MCP connections, created *disconnected* on enable | no |
| `connector` | A knowledge-source connector | **yes** |
| `vector_index` | A query-only retrieval backend for an existing remote collection | **yes** |
| `runtime_adapter` | An agent runtime implementation | **yes** |
| `model_adapter` | An LLM provider | **yes** |
| `approval_channel` | An approval-delivery channel (Telegram, WhatsApp, …) | **yes** |
| `core_extension` | In-process hook handlers | **yes**, and `trust` must be `first_party`/`verified` — a `community` capa's hooks register but the sandboxed execution path for them isn't built yet |

Every one of these works in the current codebase. The data-only types
(`skill`, `flow_template`, `tool_pack`, and the two templates) **materialise
on enable** — that's where consent happens, and disabling a capa later
never deletes what it already created.

`type` does not gate what a capa's `entry_points` may contribute: a
`tool_pack` capa can also declare a `connectors` entry point and ship a
knowledge connector in the same folder, at the same time — `microsoft365`
and `google_workspace` both do exactly this. Neither the connector
registry nor tool-pack materialisation reads `type` to decide whether to
accept a contribution; `type` only picks which manifest sections get
validated.

## Capas that ship code

The five types marked "yes" above add a Python package alongside their
manifest, in a folder named for the capa's **responsibility**, never for
the capa itself:

| `type` | Code folder |
|---|---|
| `connector` | `connector/` |
| `vector_index` | `vector_index/` |
| `runtime_adapter` | `runtime/` |
| `model_adapter` | `provider/` |
| `approval_channel` | `channel/` |
| MCP tool bridge (any `type`) | `mcp_bridge/` |

This is the direct analogue of a conventional `models/` / `controllers/` layout:
it makes a capa's layout legible without reading its manifest first, and it's why
`gdrive_source/connector/connector.py` and `s3_source/connector/connector.py`
are two entirely different files that happen to live at the same relative
path. The entry point follows the folder:

```toml
[plugin.entry_points]
connectors = "connector.connector:register"
```

```toml
[plugin.entry_points]
vector_indexes = "vector_index.index:register"
```

```toml
[plugin.entry_points]
runtime = "runtime.runtime:register"
```

The entry-point *key* (`connectors`, `vector_indexes`, `runtime`, `model_providers`,
`channels`, …) is documentation for humans reading the manifest — what you
may actually add is whatever `PluginContributions` exposes:
`add_connector`, `add_vector_index`, `add_runtime`, `add_model_provider`, `add_hook`.
`register(contrib)` is called once per process and can call more than one
`add_*` if a single capa contributes more than one thing.

A `core_extension` hook additionally has to be **declared**, not just
implemented:

```toml
[[plugin.handles]]
point = "task.before_create"
priority = 10
```

Declared *and* offered, or it never runs — otherwise a capa could hook a
point that never showed up on its own consent screen.

## Rules that apply to every code-carrying capa

- **Only trusted capas are imported.** `trust` must be `first_party` or
  `verified`. A `community` capa is never imported at all — running
  untrusted code needs sandbox isolation that doesn't exist yet. Placing a
  folder on the capas path **is** the trust decision.
- **Installing is not enough.** A code-carrying capa's `permissions` are
  only granted when it's **enabled** — an installed-but-not-enabled capa
  contributes nothing.
- **Contributions are per tenant.** The Python import is process-wide, but
  only tenants that enabled the capa can resolve its connector/runtime/
  provider/channel. Another tenant on the same installation can't use it
  or see that it's there.
- **A capa that fails to import is quarantined,** logged, and not
  retried — it doesn't take the process down, but it also doesn't silently
  half-work.
- **No hot reload.** A dropped-in folder is discovered immediately (every
  request re-scans the capas path), but its code is imported once per
  process. Changing Python code needs a process restart.

See [Manifest Reference](manifest-reference.md) for the full `plugin.toml`
schema, and [Testing Capas](testing-plugins.md) for why the shared
folder names above (`connector/`, `runtime/`, …) need a specific import
convention in tests.
