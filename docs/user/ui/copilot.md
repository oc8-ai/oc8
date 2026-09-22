# Copilot

**Surface:** floating dock in the shell (not a sidebar route)

## What it is

In-product **configuration assistant**. You describe what you want (a
department frame, an agent setup); Copilot drafts a **proposal**. You review and
apply — it never silently changes production config.

## What it is for

- Speed up setup in plain language
- Avoid blank-form syndrome for new admins
- Stay safe: Copilot gets capabilities, not raw credentials

## Where you are in the flow

```text
★ Copilot proposes → you review → applied to Departments / Agents / …
  → still test with a real run → My work for gates
```

Copilot helps **configure**. It does not replace [My work](my-work.md) approvals
for tool calls.

## What you do here

1. Open the Copilot dock.
2. Ask for something concrete (“Support agent that escalates billing”).
3. Review the proposal carefully.
4. Apply only what you trust; then test.

## Where work goes next

| After apply | Next |
|-------------|------|
| New agent / dept | [Office](office.md) / [Agents](agents.md) |
| Needs tools | [Capas](capas.md) |
| First run | Watch [My work](my-work.md) |

## Driving Copilot from outside oc8

Everything above also works from an external AI client — Claude Desktop,
claude.ai, ChatGPT, or your own tool — over oc8's external Copilot MCP
gateway, authenticated with your own API key (Settings → API keys). See
`skills-library/oc8-copilot/` in the repository for the skill that teaches
a client how to use it well.

## Related

- [Key concepts — Copilot](../key-concepts.mdx#copilot-configuration-assistant)
- [Governance](../governance-and-approvals.md)
