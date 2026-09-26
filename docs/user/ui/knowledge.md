# Knowledge

**Route:** `/knowledge` · Content: `/knowledge/bases/$id/content`

## What it is

Shared (and private) **document memory** for agents: sources you ingest, bases
you organise, chunks agents can retrieve during a run.

## What it is for

- Ground agents in your policies, product docs, SOPs
- Keep sensitive material scoped (may force a local model)
- Separate “where files come from” from “what agents may search”
- Connect an **existing** Qdrant or Postgres/pgvector collection (query-only —
  no copy into oc8) via a vector-index capa and the Credentials page

## Where you are in the flow

```text
Connector capa → ★ Knowledge (sources → bases) → link to dept/agent → used in runs
Vector-index capa → ★ Knowledge (connect existing index) → same grants / retrieval
```

## Tabs

| Tab | Purpose |
|-----|---------|
| **Data Sources** | Ingest pipelines (connectors) |
| **Knowledge Bases** | Searchable collections agents attach to (ingested or remote index) |

## What you do here

1. Add or open a data source; wait for ingest — **or** create a base with
   “Connect existing vector index”, pick the capa, choose a Credential, and
   map collection/table fields.
2. Create or open a knowledge base; attach content (ingest path only).
3. On [Agents](agents.md) / department settings, grant KB access.
4. Test with a question that should retrieve a known chunk.

Secrets for remote indexes live under [Credentials](credentials.md) — never
paste API keys or database passwords into the knowledge base config.

## Where work goes next

| Situation | Next |
|-----------|------|
| Source needs a connector | [Capas](capas.md) / [Integrations](../integrations/index.md) |
| Agent should use the KB | Agent Configuration / Memory |
| Retrieval looks wrong | Activity / Live Log for the run |

## Related

- [Key concepts — Knowledge base](../key-concepts.mdx#knowledge-base)
- [Agents](agents.md)
