# Developer documentation — Capas

Build **extensions** for oc8: connectors, tool packs, agent templates, skills,
runtimes, approval channels, and [Claude-compatible plugins](claude-plugin-capas.md).

If you change oc8 **core** (runtime, authz, database), see
[Contributing](../contributing/index.md) instead.

## Start here

| Step | Document |
|------|----------|
| 1. Understand capa types | [Plugin types](plugin-types.md) |
| 2. Build your first capa | [Tutorial: first capa](tutorial-first-plugin.md) |
| 3. Manifest reference | [Manifest reference](manifest-reference.md) |
| 4. Test locally | [Testing capas](testing-plugins.md) |

## Guides

| Topic | Document |
|-------|----------|
| Architecture (author's view) | [Architecture overview](architecture-overview.md) |
| Manifest fields (`plugin.toml`) | [Manifest reference](manifest-reference.md) |
| Capa types and contributions | [Plugin types](plugin-types.md) |
| Dependencies and requirements | [Dependencies and requirements](dependencies-and-requirements.md) |
| Guardrails and permissions | [Guardrails and permissions](guardrails-and-permissions.md) |
| Setup forms and OAuth | [Setup forms and OAuth](setup-forms-and-oauth.md) |
| Packaging and distribution | [Packaging and distribution](packaging-and-distribution.md) |
| Claude Agent SDK plugins | [Claude plugin capas](claude-plugin-capas.md) |
| Testing | [Testing capas](testing-plugins.md) |
| Measuring a model in the chat harness | [Chat harness evals](chat-harness-evals.md) |

## Exhaustive on-disk reference

The flat reference for folder layout, lifecycle, and every manifest field:

→ [`capas/README.md`](https://github.com/oc8/oc8/blob/main/capas/README.md)

## Quick start

1. Create `capas/<capa_id>/` — folder name **must** equal manifest `name`.
2. Add `plugin.toml` and/or a [Claude plugin layout](claude-plugin-capas.md).
3. Discovery rescans on request — restart not required. Install and enable per
   tenant from the Capas UI; accept permission consent.

## Capa types (summary)

| Type | Purpose |
|------|---------|
| `tool_pack` | MCP connections (disconnected until configured) |
| `agent_template` | One hireable agent |
| `department_template` | Team + frame starter |
| `skill` | Procedures materialised on enable |
| `connector` | Knowledge source (RAG ingest) |
| `runtime_adapter` | Alternative agent runtime (Docker) |
| `approval_channel` | Messenger approvals |

## Tests

```bash
cd backend
uv run pytest tests/plugins/test_discovery.py -q
uv run pytest tests/plugins/test_claude_plugin_bridge.py -q
```

Fixtures: `backend/tests/plugins/fixtures/`.

## Related

- [User docs](../user/index.md) — for operators using your capa
- [Contributing](../contributing/index.md) — core PRs
