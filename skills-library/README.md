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

## Quick install

- **Claude Code / Claude Desktop (local agent mode, filesystem access):**
  point it at this skill's directory directly — e.g. symlink or copy
  `skills-library/oc8-copilot/` into `~/.claude/skills/oc8-copilot/`. Claude
  picks it up the next time it lists available skills; no packaging step
  needed.

- **claude.ai (web, no filesystem access):** package the skill into a
  distributable `.skill` file, then upload it under
  **Settings → Capabilities → Skills → Upload skill**:

  ```bash
  python package_skill.py skills-library/oc8-copilot
  ```

  (`package_skill.py` ships with Anthropic's `skill-creator` skill.)

- **ChatGPT:** there's no skill-upload mechanism — create or edit a Custom
  GPT instead, and paste `oc8-copilot/references/chatgpt-instructions.md`
  into its instructions field.

Either way, installing the skill only gets an AI client the *instructions*
— `oc8-copilot/references/getting-started.md` still walks the user through
minting an oc8 API key and connecting it to `/mcp/external` before any
operation in the skill can actually run.
