# skills-library

Skills for *external* AI clients (Claude Desktop, claude.ai, ChatGPT, or
your own integration) that configure or operate an oc8 tenant on a human's
behalf — as opposed to `capas/*`'s `skill`-type capas, which are procedures
an oc8 *agent* loads during its own runs (see `docs/user/ui/skills.md`).
Different audience, different lifecycle, hence a separate top-level
directory rather than another capa.

Each subdirectory is one skill:

- **[`oc8-copilot/`](oc8-copilot/SKILL.md)** — drives oc8's external Copilot
  MCP gateway (`/mcp/external`) to set up departments, agents, missions,
  guardrails, triggers, plugins, and integrations from a plain-language
  request. Ships a Claude `SKILL.md` and an equivalent
  `references/chatgpt-instructions.md` for platforms without native Skill
  support.

Install the Claude version by pointing Claude at this directory (or
packaging it with `skill-creator`'s `package_skill.py`); for ChatGPT, paste
the relevant `references/chatgpt-instructions.md` into a Custom GPT's
instructions.
