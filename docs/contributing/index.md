# Contributing to oc8

This section is for anyone working on oc8's **core** — the agent runtime,
the capa loader, the authorization layer, the database layer, the
frontend shell.

Documentation hub (all audiences): [docs/index.md](../index.md)

If you're building a capa instead — a connector, an MCP tool bridge, a
runtime adapter, a model provider, an approval channel, or a data-only
bundle — see [Developer](../developer/index.md) instead. Most changes to
oc8 belong there, not here: per the
[microkernel principle](architecture.md#the-microkernel-principle), a
change specific to one piece of software or one use case belongs in a
capa, not in core.

## Where to start

1. **[Core Architecture](architecture.md)** — the microkernel principle,
   the subsystem map, the agent execution loop, and multi-tenancy. Read
   this first.
2. **[Coding Guidelines](coding-guidelines.md)** — lint/type-check
   configuration and the testing discipline this codebase expects.
3. **[Git Guidelines](git-guidelines.md)** — commit message conventions
   and pre-PR checks.
4. **[Core UI Translations](translations.md)** — add or update a language
   for oc8's own UI via a `.po` catalog, no code change required.
5. **[Translating on translate.oc8.ai](https://docs.oc8.ai/contributing/weblate)** —
   the translator guide for the hosted Weblate workflow.

## Opening a pull request

1. Fork or branch, make your change, and follow
   [Coding Guidelines](coding-guidelines.md) and
   [Git Guidelines](git-guidelines.md).
2. Run the full check locally before opening the PR — lint, type-check,
   and the full test suite. A PR that doesn't pass locally won't pass CI
   either.
3. Describe **why**, not just what, in the PR description. If the change
   touches core, say which mechanism it adds or fixes and why it couldn't
   be a capa instead.

## Reporting a bug

Open an issue with: what you expected, what happened instead, and the
smallest reproduction you can manage. If the bug is in a specific capa
rather than core, say so — it changes who's likely to pick it up.
