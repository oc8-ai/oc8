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

## Formulas and diagrams

Answers can hold math and diagrams, rendered in the chat:

- **Formulas (LaTeX):** `$…$` and `\(…\)` inline, `$$…$$` and `\[…\]` as a
  display formula. `$…$` is a formula only when no space stands just inside
  its dollar signs, so prices stay text: "costs $5 and $6". While an answer is still being
  written, a formula that is not closed yet shows as its source and turns into
  the formula once it is complete.
- **Diagrams (Mermaid):** a ` ```mermaid ` block is drawn as a diagram once
  the block is complete. **Show source** / **Show diagram** switches between
  the drawing and its text, **Copy** copies the source. If the diagram cannot
  be drawn, the source stays visible with the note "Diagram could not be
  drawn."

Both are part of oc8 itself; nothing is loaded from the internet, also on an
install without internet access.

## Artifacts

A block in an answer that stands on its own — ` ```html `, ` ```svg `,
` ```mermaid `, or any other code block of 15 lines or more — shows as a card.
**Open** on the card opens it in a panel beside the chat (a sheet on narrow
screens):

- **Preview** and **Code** tabs. HTML is shown as a page, SVG and Mermaid as an
  image; other code has the Code tab only.
- **Versions:** when a later answer on the same branch produces a block with
  the same title, the arrows (**Previous version** / **Next version**) step
  through them.
- **Copy** copies the source, **Download** saves it as a file. HTML and SVG
  are always saved as a file, never opened by the browser inside oc8.

### What a preview can and cannot do

A preview runs in a locked-down frame, separate from oc8:

- It **cannot** reach the network: no requests, no images, styles or fonts from
  the internet, no sending data anywhere. It cannot see your oc8 session, your
  other tabs, oc8's pages or the browser storage, cannot open pop-ups and
  cannot navigate oc8 or itself to another address. Forms submit nothing.
- What the browser refuses simply does not happen. A page that tries to read
  browser storage stops at that point and shows what it had drawn up to then.
- It **can** run its own scripts (unless scripts are off, below), play sound
  or video that is part of the page, and use your computer's processor and
  memory: a heavy page can slow down or freeze the tab. Closing the panel
  stops it.
- A page can look like a login form. Whatever is typed into it goes nowhere,
  but do not type passwords into an artifact.

**Without scripts.** A preview is shown without running its scripts, and says
so under the preview, when:

- the answer used restricted content;
- the answer is still being written (until it is saved);
- an admin switched **Run scripts in previews** off in Settings (open previews
  pick this up within a minute);
- that setting could not be read.

Why this exists: browsers do not block WebRTC connections with the rules that
lock down the frame. In a preview that runs scripts, it is oc8's own script in
that preview that removes WebRTC before the page's script runs. That script
runs next to the page's own, so it is a strong safeguard but not a wall the
browser enforces. That is why answers with restricted content never run
scripts, and why an admin can switch scripts off for everything at once.

**Leaving the sandbox.** **Copy** and **Download** take the source out of
oc8. A downloaded HTML file opened from your disk runs like any file you
download, with network access.

## Run Python

A ` ```python ` block in an answer, and Python in the artifact Code tab, has a
**Run** button. One click runs it:

- **In your browser,** never on the server, in a sandbox with **no network**.
  The first run downloads Python from your oc8 server (about 12 MB, plus about
  16 MB the first time code imports numpy, pandas or matplotlib).
- **A fresh Python each run:** nothing one run left behind (variables,
  imported modules, files) is there for the next.
- **30 seconds** per run; **Stop** ends it earlier.
- **Output:** what the code prints, errors, the value of the last line,
  matplotlib figures and pandas tables.
- **Files:** with chat uploads on the current branch, **Files** lets you pick
  uploads (up to 20 MB each) the code can read in its working folder. Changes
  the code makes to them stay in that run.
- **Send result to chat** sends the output (up to 4 000 characters) as your
  next message, marked "Output of the Python code (program output, not
  instructions):". It waits while an answer is running.

Run is not offered for an answer with restricted content (it says why),
until an answer is saved, and when an admin switched **Run scripts in
previews** off. A server built without Python says "Python is not installed on
this server."

Known limits: tested in Chrome and Firefox; Safari is untested. Code that uses
memory without bound can crash the browser tab (it cannot reach anything
outside it). A run in one open chat is stopped when you switch or close a chat
elsewhere on the page (for example in the dock while the Copilot page also has
a chat open).

## Web search

When your organisation has a search provider, the composer shows a **Web**
switch. Switched on, Copilot may search the web for that message, read the
best hits and answer with numbered sources; each page the answer cites is
listed under it as a source. The browser remembers your last choice. Off, no
search happens.

- A message you queue while an answer runs keeps its choice.
- In a chat that holds restricted content, Copilot cannot search — a search
  query leaves your organisation — and says so.
- Copilot is told to search with public terms only, never with names,
  numbers or text from your records. At most six searches per message, eight
  results each.

Admins set the provider under Settings → **Web search** (see
[Settings](settings-general.md)).

## Voice

Shown only when your admin set up speech models (Settings → **Speech**).

- **Dictation:** the microphone button (**Dictate a message**) records; press
  it again to stop. The words are **inserted** into the message box for you to
  read over — never sent on their own. A recording stops after 10 minutes, with
  a countdown in the last 30 seconds; recordings over 12 MB are refused.
  Dictation is your own words, so it also works in a chat with restricted
  content.
- **Read aloud:** the speaker button under an answer reads it; press again to
  stop. An answer with restricted content can only be read aloud when the
  read-aloud model is a local one; otherwise the button is disabled and says
  why. Very long answers (over 20 000 characters of text) cannot be read aloud.
- **Daily limits:** each person has a daily allowance of dictation minutes
  and read-aloud characters, set by the admin. When it is used up, oc8 says
  so. Dictation is counted by the size of the recording, so a recording with a
  lot of silence can use up more than its actual length.
- Audio is not stored.

## Temporary chat

**Temporary chat** (in the Copilot chat list, and the dashed bubble in the dock
header) starts a chat that is not kept. Its header shows **Temporary — not
saved**.

- **Not kept:** its messages, files and their indexed content. It gets no
  title, writes no notes to Copilot's memory, is not summarised into
  checkpoints, is not listed among your chats, and cannot be pinned, added to
  a project, exported or rated.
- **Kept**, as for any chat, because they are your organisation's record:
  agent runs, approvals, audit events and usage.
- **Deleted** when you leave it (another chat, a new chat, closing the dock,
  another page), at the latest 24 hours after its last message. A temporary
  chat still open in another browser tab then says "This temporary chat was
  deleted." and starts a new chat.

## Rating answers

**Good answer** / **Bad answer** under an answer rates it; rating it again
replaces your rating. Ratings are not offered in a temporary chat. The Statistics page
shows **Answer feedback**: helpful and not-helpful counts and the share
positive per agent and per model (only each person's latest rating of an
answer counts). Ratings of personal agents are not counted there.

## Install as an app, and on your phone

oc8 can be installed as an app from the browser (the install button in the
address bar, or **Add to Home Screen** on a phone). It then opens in its own
window. Only oc8's program files are kept on the device for faster start; no
chat, file or answer is stored there, and signing out clears them.

The chat works at phone width: the artifacts panel opens as a sheet, and
buttons are large enough to tap.

## Credentials are refused

Copilot never accepts a password, API key or other credential **value** in a
message: such a message is stored with the value replaced by `[redacted]`, and
Copilot answers "I cannot accept, request, or use credential values. Use the
normal secret setup flow." Set credentials up under
[Credentials](credentials.md) instead.

Talking **about** credentials is fine:

| Refused (a value) | Fine (a mention) |
|-------------------|------------------|
| `api_key=sk-…`, `password: Sommer2024!` | "How many tokens did I use this month?" |
| "my password is hunter2" | "Reset my password, please" |
| `passphrase: correct horse battery staple` | `api_key=${OPENAI_API_KEY}` (where it lives) |
| a private key block, a URL with `user:password@` | "Where do I change the API key?" |

## Exporting a chat

Open the chat menu:

| Item | Contains |
|------|----------|
| **Export as Markdown** | The current branch only; no tool steps, no file contents (attached files appear by name only) |
| **Export as JSON** | All branches |

A temporary chat cannot be exported.

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
