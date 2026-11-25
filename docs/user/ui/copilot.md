# Copilot

**Surface:** the full **My Copilot** page (`/copilot`) and the floating **dock**
in the shell, which links to the full page

## What it is

Your **AI workplace**: a general-purpose assistant you can talk to in the web
chat (and by Telegram). It answers questions, writes, researches and analyses,
and hands work to the right agent when one exists. For changes to the setup
itself it drafts a **proposal** that you review and apply — it never silently
changes production config.

## What it is for

- Get everyday knowledge work done without picking a tool first
- Reach the agents you already have, by describing the work instead of
  navigating to them
- Speed up setup in plain language (new agents, departments, guardrails)
- Stay safe: Copilot gets capabilities, not raw credentials

## How it picks who does the work

1. **An existing agent first.** If an agent that fits the request exists and you
   are allowed to run it, Copilot names it and delegates the work. If the fit is
   unclear, it offers that agent instead of guessing.
2. **Otherwise it does the work itself** — questions, writing, research,
   analysis and planning are always its own.
3. **When the same kind of work keeps coming back,** it suggests building an
   agent for it. It builds one only after you agree, and the result is a
   proposal you review.

If nothing fits and it cannot do the work, it says so openly.

Copilot does not open with a status summary. Ask when you want one (“What is
waiting?”), or open the work panel (the **Show work panel** toggle on the
Copilot page) and [My work](my-work.md).

## Where you are in the flow

```text
★ Copilot (ask → delegate or do it) → agents run → My work for gates
★ Copilot proposes setup changes → you review → applied to Departments / Agents / …
```

Copilot does not replace [My work](my-work.md) approvals for tool calls.

## What you do here

1. Open the Copilot page, or the dock for a quick question.
2. Ask for something concrete (“Summarise this contract”, “Support agent that
   escalates billing”).
3. For setup proposals: review carefully, apply only what you trust, then test.

## Long chats

A long conversation does not run out of room. When a chat grows past what the
model can hold, Copilot summarises the older part into a **checkpoint** and
carries on with the summary plus the recent messages. Summaries that came from
restricted content are only replayed to local models.

## Approvals

When Copilot wants a tool that needs your approval, the card appears in the
chat. Approve it and the conversation **continues with its context**, as if
nothing had paused. If you reject, you can give a **reason**; Copilot sees it
(“rejected by <name>: <reason>”) and adapts instead of retrying blindly.

On your own approval card for a **write**-level tool you can choose **Always
allow (for me)**. It is never offered for delete, irreversible or outward-facing
tools, nor for a department stage. Review and revoke your rules under
**Profile → Always-allowed tools**.

## Sending while an answer runs

While Copilot is still answering, the composer offers two choices:

- **Steer now** hands your message to the running answer before its next step.
  It is offered only when the runtime accepts inputs.
- **Send after** queues your message as the next one, sent when the answer is
  done. Until then you can edit or cancel it. If the run fails, the queued
  message still becomes the next turn after the failure answer; if you stop the
  run, it is cancelled.

A plain send while an answer runs is queued the same way. During an open
clarification question the composer answers that question instead. Only the
chat's owner can queue messages.

## Editing and branches

- **Edit a sent message** with the Edit button under your own message (shown on
  hover, always visible on touch). It opens an inline editor; resending it: the chat continues on a new branch, the
  original stays.
- A message with several versions shows **‹ n/m ›** — use the arrows to switch.
- **Regenerate** (icon button in the answer's action row) to get another variant of it; switch between variants
  the same way.

## Attaching files

Small files go straight into the conversation. Above about 20 000 characters a
file is **indexed in the background** into a hidden knowledge base that belongs
to that chat. It never shows up in [Knowledge](knowledge.md) lists. Copilot can
then search it and read it page by page, so a long document does not have to fit
into one message.

- Until indexing finishes, Copilot tells you the file is still being indexed.
- If indexing is not possible — for example no embedding model is configured,
  or the agent runs on a local model and no local embedding model exists — the
  file is added inline instead, **truncated**, with a note saying so.
- Above 2 000 000 characters a file is stored but not indexed, and is handed
  over inline, truncated, like the other fallbacks. Inline text is capped at
  200 000 characters.
- Deleting a file, or the whole chat, erases its indexed content.

## Exporting a chat

Open the chat menu:

| Item | Contains |
|------|----------|
| **Export as Markdown** | The current branch only; no tool steps, no file contents (attached files appear by name only) |
| **Export as JSON** | All branches |

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

- [Key concepts — Copilot](../key-concepts.mdx#copilot-ai-workplace)
- [Governance](../governance-and-approvals.md)
