# Design System — oc8

The source of truth for every visual and UI decision in `frontend/`. It
codifies the existing oc8 brand (tokens in `frontend/src/styles.css`) and sets
the rules that keep the product consistent. A new screen extends these rules;
it does not invent its own.

## Product Context

- **What this is:** a governed multi-agent platform. AI agents work in
  departments, inside guardrails, with approvals and an audit trail. Every
  member also has a personal Copilot.
- **Who it's for:** admins and department leads who set the rules, and
  members who work with agents every day.
- **Space:** agent orchestration and governance. Peers in feel: Linear,
  Stripe Dashboard, Odoo, Apple System Settings.
- **Project type:** dense web app (dashboard, settings, chat, inbox).

## The one thing to remember

**Calm and in control.** Agents do the work and the person stays in charge.
The interface is quiet by default and loud only when a human is needed. Every
rule below serves this.

## Principles

1. **One mechanism, everywhere (Odoo style).** One concept has one name, one
   component and one place. Example: there are only *guardrails*, on three
   levels (department, agent, me), shown with the same table everywhere.
2. **Clarity, deference, consistency (Apple HIG).** Content first and chrome
   second. Fixed values are quietly greyed, without locks or warning colours.
3. **Progressive disclosure.** Show the 3–5 things that matter. Put the rest
   under "More" or in a sheet, not on a new page.
4. **Agents are colleagues, not magic.** An agent shows a name, role, scope,
   permissions and recent activity, like an employee record. Never use
   sparkles, AI glows or decorative gradients on content.
5. **Control is visible.** Every place an agent can act shows its level in
   the same four-word vocabulary (see *Guardrail levels*). Every decision has
   an audit line: who, what, when, why.
6. **Only "needs you" is loud.** Items waiting on a person get the orange dot
   and a count. Everything else stays neutral.

## Aesthetic Direction

- **Direction:** calm workspace. Neutral surfaces, high information density,
  hairlines instead of shadows.
- **Decoration level:** minimal. The brand gradient is the only ornament, and
  it is used sparingly (see *Color*).
- **Mood:** serious software that is pleasant to spend a day in. Trust beats
  wow.
- **Reference sites:** linear.app, vercel.com/geist, primer.style,
  developer.apple.com/design/human-interface-guidelines, odoo.com.

## Typography

| Role | Font | Notes |
|---|---|---|
| Headings (h1, h2, `.font-serif`) | **Space Grotesk** 600, tracking −0.02em | Despite the token name `--font-serif`, this is a grotesk. |
| Body and UI | **Inter** 400/500, features `cv02 cv03 cv04 cv11` | 500 is the emphasis weight. Use 600 for titles only. |
| Data, IDs, values, code | **JetBrains Mono** 400 | Use tabular numerals for amounts. |

Fonts are self-hosted from `frontend/public/fonts/` (no CDN).

**Scale.** Use only these sizes. Arbitrary sizes such as `text-[10px]`,
`text-[11px]` or `text-[9px]` are not allowed in new code.

| Token | Size / line height | Use |
|---|---|---|
| `h1` | 24 / 32, Space Grotesk 600 | Page title (one per page). |
| `h2` | 20 / 28, Space Grotesk 600 | Section or sheet title. |
| `title` | 16 / 24, Inter 600 | Card, row or object title. |
| `body` | 14 / 20, Inter 400 | Default text, buttons, inputs, table cells. |
| `caption` | 12 / 16, Inter 400, muted | Secondary lines, effect sentences, timestamps. |
| `micro` | 11 / 14, Inter 500, uppercase, tracking 0.08em, muted | Labels above fields, table headers, small status. One style only. |
| `mono` | 12, JetBrains Mono | IDs, amounts, technical values. |

## Color

**Approach:** restrained. Neutral first, and colour carries meaning. Values
are the tokens in `styles.css` (oc8 Brand Style Sheet v1.0).

| Token | Dark / Light | Meaning and use |
|---|---|---|
| `--background` | `#060b14` / `#f6f8fa` | Page. |
| `--panel`, `--card`, `--popover` | `#0e1220` / `#ffffff` | Surfaces. |
| `--muted` (= secondary, accent) | `#141827` / `#eef1f6` | Hover, subtle fills. |
| `--muted-foreground` | `#8a94a6` / `#6b7280` | Secondary text. |
| `--border` | white 8 % / navy 10 % | Hairlines. |
| `--primary` | `#286bff` | **The** primary action of a view and the current selection. Nothing else. |
| `--ring` | `#7a4dff` | Focus ring only. |
| `--status-running` | `#20d4bf` | Running, done, success. |
| `--status-warning` | `#ff9a3c` | **Waiting on you.** Also attention and warnings. |
| `--status-error` / `--destructive` | oklch hue 25 | Failed, rejected, destructive. |
| `--status-paused` | grey | Paused, off. |

Rules:

- **Status is never colour alone.** Always pair it with a word, and a dot or
  icon. Use the shared status pill, not a hand-rolled one.
- **No raw palette colours** (`amber-*`, `emerald-*`, `blue-500`, hex in
  components). Use tokens. If a meaning has no token, add one in
  `styles.css` (`:root` and `.dark`), register it in `@theme`, and log it
  below.
- **At most one attention colour per region.**
- **Brand gradient** (`#26d9ff → #2775ff → #7a4dff → #e84ba5 → #ff9a3c`,
  135°) is the brand's signature. It is allowed only on:
  - the logo, the octopus and auth/onboarding surfaces;
  - the **highlight button**: the one especially emphasised action, at most
    one per screen and used rarely. Examples: "Create your first agent" in an
    empty workspace, or the final step of onboarding.
  - Ordinary primary buttons are **flat** Electric Blue. Badges, progress
    bars and chat bubbles never use the gradient.
- **Dark mode is the default** (class toggle on `<html>`). Every token has a
  light and a dark value. Never hardcode `bg-white`, `text-black`, etc.

## Buttons

| Variant | Look | When |
|---|---|---|
| `highlight` | Brand gradient, white text | The one standout action on a screen. Rare. |
| `default` (primary) | Flat `--primary`, white text | The main action of a view. One per view. |
| `outline` / secondary | Panel with a hairline border | Everything else that matters. |
| `ghost` | Transparent | Tertiary actions such as "Change rule" or "More". |
| `destructive` | Red text and border; filled only in the confirm step | Delete, revoke. |

Minimum target is 36 px tall in dense tables and 44 px for touch and menu
options. Button labels say what happens ("Approve", "Send mail"), never
"OK".

## Spacing

- **Base unit:** 4 px. Half-steps (2 px) only inside controls.
- **Density:** compact.
- **Use:** rows `px-3 py-2 gap-2`; panels and sheets `p-5`; stacked sections
  `space-y-4` or `space-y-6`; page gutter 24 px (16 px on mobile).
- No arbitrary paddings (`p-[13px]`).

## Layout

- **One skeleton per object.** Every agent, department, policy, approval and
  run uses the same layout: **header** (title, status pill, actions on the
  right), **content**, and an **activity/audit column** (on mobile it goes
  below). New object pages extend this skeleton.
- **Sheets for details and editing**: right side on desktop (≥ 480 px wide),
  full-height bottom sheet under 640 px. **Dialogs only for confirmation**
  (AlertDialog) and short creation flows.
- **Tables:** use `components/ui/table`. Headers use the `micro` style, there
  is a row hover, and the table never scrolls sideways on the page. On
  mobile the name and the key value stay on one line and details wrap below.
- **Max content width:** 1120 px for reading pages. Dashboards may use the
  full width.
- **Radius:** `rounded-md` (8 px) for controls, `rounded-lg` (14 px,
  `--radius`) for panels, cards and sheets, `rounded-full` for dots, pills
  and avatars. Do not use bare `rounded`.
- **Shadow:** only floating layers (menus, popovers, dialogs, sheets) get a
  shadow. Surfaces on the page get a hairline border instead.

## Guardrail levels (shared vocabulary)

The same four levels appear wherever an agent's authority is shown or set:
*Off* · *Read only* · *Needs approval* (on department and agent) / *Ask me
first* (for me) · *Allowed*. Details such as thresholds, single actions and
conditions go under "More", with a quiet "with limits" hint.

The level is chosen with a pop-up button, not a row of segments. A level
the ceiling forbids is greyed, with "Set by department" or "Set by agent",
and stays focusable.

## States

Every view specifies these states before it is built:

| State | Rule |
|---|---|
| Empty | Say what this is, why it is empty and offer one action, e.g. "No guardrails of your own yet. Say what should never happen without you." Never just "No items." |
| Loading | Use a skeleton in the shape of the content. Use a spinner only inside a button that is running its own action. |
| Error | Show it inline, at the place it happened, in one sentence: what went wrong and what to do. A failed save reverts the control. A toast may only confirm; it never carries the only error message. |
| Waiting on you | Orange dot and count, wherever the item appears (inbox, chat, Copilot tab). |
| Success | Show it quietly: the control shows the new state and the effect sentence updates. A toast is optional. |

## Motion

- **Approach:** minimal and functional. Motion shows a change of state and
  never decorates.
- **Duration:** 150–200 ms for feedback and 200–300 ms for sheets and
  popovers. Easing: `ease-out` to enter, `ease-in` to exit.
- Never more than two animations at once. No looping animation except an
  in-progress indicator.
- **`prefers-reduced-motion` is respected globally** (one rule in
  `styles.css`), not per component.
- The octopus companion (`oc8-docking-*`) is the one place with personality.
  It may be playful, but never on top of content and never on governance
  screens.

## Icons

- lucide-react. Sizes: `size-4` (16 px) is the default, `size-3.5` goes next
  to `caption` text, and `size-5` is for page headers. Prefer the `size-*`
  shorthand over `h-* w-*`.
- An icon never replaces a label for an action that is not universal.

## Accessibility

- Contrast ≥ 4.5:1 for body text and ≥ 3:1 for large text, in both themes.
- Focus rings are always visible (`--ring`, 2 px, offset 2).
- Everything can be reached and operated by keyboard. Pop-ups use proper
  roles (`radiogroup`, `menu`): arrow keys move, Enter picks, and Esc closes
  and returns focus to the trigger.
- Changing text (effect sentences, decisions) sits in `aria-live="polite"`.
- Action labels carry their object ("Approve: send mail to kunde@x.de").

## Copy

- Bilingual through `t("English", "Deutsch")`, with the catalogue in
  `i18n/de.po`. A short English msgid that already exists with another
  meaning (e.g. "you") needs a distinct msgid.
- **German uses "du"** (informal), everywhere and consistently: "Deine
  Guardrails", "Was soll nie ohne dich passieren?".
- Short sentences, active voice, no exclamation marks, no "Oops". Name the
  thing ("agent", "approval"), never "AI magic".
- Numbers and amounts use the German format in German (`12.480,00 €`).

## Known gaps to close (from the 2026-11-08 inventory)

New code follows this file. These existing spots are migrated when touched:

- About 500 arbitrary text sizes (`text-[10px]`, `text-[11px]`, `text-[9px]`)
  → the scale above.
- 41 raw `amber-*` / `emerald-*` classes and hex persona swatches → tokens.
- `status-*` used through `[color:var(--status-*)]` arbitrary values → real
  utilities and a shared status pill.
- 20 raw `<table>` elements → `components/ui/table`.
- 43 loading spinners → skeletons, except inside buttons.
- `.bg-primary` gradient on every filled primary surface → flat primary plus a
  `highlight` button variant.
- German "Sie" (about 120 places) → "du".
- Reduced motion handled only for the docking station → one global rule.

## Decisions Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-11-08 | Initial DESIGN.md, codifying the existing brand (no redesign) | Created with /design-consultation, based on the brand sheet in `styles.css`, a code inventory and research (Linear, Odoo, Apple HIG, Stripe, Geist, Primer, agent-approval UX). |
| 2026-11-08 | Guiding principle: "calm and in control" | Chosen by the product owner. Trust over wow for a product that lets agents act. |
| 2026-11-08 | Brand gradient only on the logo, auth/onboarding and the rare `highlight` button; primary buttons are flat | Product owner: a standout action should still carry the gradient. Everywhere else it competed with status colours and made the UI loud. |
| 2026-11-08 | German copy uses "du" | Fits the personal Copilot and the new guardrail texts. The mix was roughly 55 % Sie and 45 % du. |
| 2026-11-08 | One type scale (24/20/16/14/12/11) and one micro-label style | Replaces about 500 arbitrary sizes and four label variants. |
