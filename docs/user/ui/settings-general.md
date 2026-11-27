# Settings — General

**Route:** `/settings` · **Settings** section → General

## What it is

Organisation-level setup: pilot checklist, org profile, secrets panel,
backup/restore entry points, and guided setup.

## What it is for

- Finish first-time configuration without hunting menus
- Org name / basics
- Operator backup actions (see also Install docs)

## Where you are in the flow

```text
★ General / checklist → Models · Credentials · Capas → Office ready
```

## Panels you will see

| Panel | Purpose |
|-------|---------|
| Pilot checklist | Links to models, credentials, capas |
| Organization | Tenant profile |
| Credentials / secrets | Secret store entry (see also [Credentials](credentials.md)) |
| Chat previews | **Run scripts in previews** — see below |
| Web search | The provider for Copilot's **Web** switch — see below |
| Speech | Dictation and read-aloud models and daily limits — see below |
| Backup and restore | Operator hooks — full runbook in Install docs |
| Guided setup | Wizard-style continuation |

## Chat previews

**Run scripts in previews** (on by default) lets pages and apps the assistant
writes run their scripts in the sandboxed preview beside the chat. Turned off,
every preview is shown without scripts and **Run** for Python is not offered.
Open previews pick the change up within a minute. Answers with restricted
content never run scripts, whatever this says. Why you might switch it off:
see [Copilot — Artifacts](copilot.md#artifacts).

## Web search

The provider Copilot searches with when someone switches **Web** on for a
message:

| Choice | Meaning |
|--------|---------|
| **Platform default** | The search your oc8 platform provides (shown when the operator set one up; it says "Provided by your oc8 platform"). Its address and key are not shown. Without one, this reads **Not set up**: no Web switch. |
| **Off** | No web search for your organisation, platform default or not. |
| **SearXNG** | Your own SearXNG: its address. |
| **Brave** / **Tavily** | An API key. It is stored with your organisation's secrets and never shown again (the field says "saved"); leave it empty to keep it. Choosing a provider without a key removes the stored one. |

An internal SearXNG address (a host on your network) works only when the
operator allowed it (see the deployment guide, "Reaching systems on your own
network").

## Speech

Pick a **Dictation model** and a **Read-aloud model** (speech models are added
under [Models](models.md); **None** switches that half off), and the daily
allowance per member: **Dictation minutes per day** (default 60) and
**Read-aloud characters per day** (default 200 000). 0 switches it off for
members. Dictation is counted by upload size, so recordings with a lot of
silence can exceed the minutes. Speech usage shows as 0 in [Costs](costs.md)
for now.

## What you do here

1. Walk the checklist after [Quickstart](../quickstart.md).
2. Confirm org details.
3. Do not treat this page as the daily inbox — that is [My work](my-work.md).

## Where work goes next

| Checklist item | Next |
|----------------|------|
| Model | [Models](models.md) |
| Tools | [Capas](capas.md) |
| People | [Users](users.md) |
| Host backup | [Install and maintain](../install-and-maintain/index.md) |

## Related

- [Welcome](welcome.md)
- [Quickstart](../quickstart.md)
