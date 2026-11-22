# Manifest Reference

## `plugin.toml`

The `[plugin]` table maps 1:1 onto oc8's manifest schema:

```toml
[plugin]
name = "vertrieb_bundle"      # must equal the folder name
version = "1.0.0"             # semver; installing the same version twice is rejected
type = "department_template"  # see Capa Types
trust = "first_party"         # first_party | verified | community
summary = "One line, shown in the Capas UI"
core_compat = ">=0.1"         # optional PEP 440 specifier against the core version
permissions = []               # a LIST of permission strings
depends = []                   # capabilities that must already be present, or install fails
plugin_depends = []            # OTHER CAPAS this one needs installed first
requirements = []              # PEP 508 strings this capa's IN-PROCESS code needs
```

Two things to know before you write your first one:

- **Unknown keys are a hard error.** The schema is strict — a typo fails
  loudly instead of being silently ignored. If a field isn't listed here
  or in `backend/src/oc8/capas/manifest.py`, it doesn't exist.
- **`permissions` is a list**, not a table.

`plugin.toml` itself carries only the core identity fields plus whichever
of `entry_points`, `handles`, `sandbox`, `department_template`,
`skill_template`, `skill_pack`, `flow_template`, `config`, `tools`, `mcp`,
`skills`, `triggers`, `policy`, `io`, `phases`, `hooks` your capa's
`type` actually uses. Everything else — the MCP connection, the
guardrails, the setup form — lives in its own file or folder, described
below.

## The manifest split

Discovery assembles one pydantic `Manifest` object from whichever of these
files/folders are present — nothing downstream of discovery cares that the
manifest used to be one file. You only ship the pieces your capa
actually needs.

```
capas/<name>/
    plugin.toml              # required — the slim core manifest
    tool_pack.toml            # optional — MCP connection wiring
    guardrails/                 # optional — one file per preset/library entry
    setup/                        # optional — present only if the capa has a setup form
    i18n/                           # optional — translation catalogs
    connector/ | runtime/ | provider/ | channel/    # optional — see Capa Types
    mcp_bridge/                                       # optional — MCP tool bridge
    tests/                                              # the capa's own pure-unit tests
```

### `tool_pack.toml`

If a capa exposes MCP tools, its connection wiring lives in a sibling
`tool_pack.toml`. **This file's own top level *is* the `tool_pack` table**
— there's no `[plugin.tool_pack]` wrapper to nest under. `microsoft365`'s
real file:

```toml
[[connections]]
key = "primary"
name = "microsoft365"
transport = "stdio"
server_url = ""

[connections.scopes]
read = ["mail_search", "mail_get", "..."]
send = ["mail_send", "mail_reply", "..."]

[connections.config]
command = "python"
args = ["-m", "mcp_bridge"]
outward_tools = ["mail_send", "mail_reply"]

[connections.config.env]
PYTHONPATH = "/app/capas/microsoft365"

[connections.config.secret_env]
GRAPH_ACCESS_TOKEN = "oauth:graph_access_token"
```

`outward_tools` and `scopes` are how a connection tells core which tools
reach outside the tenant and which ones are read vs. write vs. send — see
[Architecture Overview](architecture-overview.md#how-a-capas-tools-get-called-during-a-real-run)
for how core reads these at call time.

### Files in and out of a tool

A tool never pushes file bytes through the model. Core moves them as files
of the current run (`FileAttachment`, `owner_type="agent_run"` — the same
files `write_output_file` produces and the agent's workspace lists), in both
directions, for any MCP tool (`oc8/agent/run_files.py`):

**Returning a file.** Return it as MCP binary content: an `EmbeddedResource`
whose `resource` is a `BlobResourceContents` (the URI's last path segment is
the file name, e.g. `attachment:///invoice.pdf`), or `ImageContent`. Core
stores each one as a file of the run, under the same 25 MB cap and content
type allowlist as every other run file, and the model reads a line such as

```text
[File kept for this run: "invoice.pdf" (application/pdf, 120.4 KB), file id 0190…. Pass this id where a tool takes a file.]
```

in its place. A type the allowlist refuses, or a call made outside an agent
run (the connection test, say), is named in a `not kept` line instead; the
bytes are never handed to the model. Put whatever else the model should
read (ids, sizes) in a `TextContent` next to it.

**Taking a file.** Mark the parameter in the tool's input schema with
`"x-oc8-file": true`, on a property or on an array's `items`, at any depth:

```python
"attachments": {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"file": {"type": "string", "x-oc8-file": True}},
    },
},
```

The model passes a file id there (`file:<id>` and an exact file name work
too). Before the call reaches the bridge, core replaces the value with

```json
{"file_id": "…", "name": "report.pdf", "mime_type": "application/pdf",
 "size": 12345, "content_base64": "…"}
```

so the bridge receives the file and needs no access to oc8's storage. The
marker is removed from the schema the model sees, and the description says
a file id goes there. A run may hand over its own files, those of the same
agent's other runs, the agent's instruction files and the files attached in
the run's own chat — always within its tenant. Anything else, and any file
reference outside an agent run, fails the call before it leaves, with the
same answer whether the file exists or not.

## Triggers: `[[plugin.triggers]]`

A capa declares the events an agent's trigger may listen on. The short form
is a plain string list (`triggers = ["item.created"]`); the table form adds
how the trigger editor shows the event and, for an event core polls, the
settings a trigger on it takes. `microsoft365`'s real declaration:

```toml
[[plugin.triggers]]
type = "mail.received"          # the event type; its source is the capa's name
label = "New email"
description = "Starts the agent when a new mail arrives ..."
polled = true                   # core polls for it through this capa's poller
editor_label = "On new email"   # the trigger editor's tab; `label` when empty

[[plugin.triggers.config]]      # at most 16 settings
key = "mailbox"
label = "Mailbox"
default_from = "default_user"   # the value the login's setup gave this field

[[plugin.triggers.config]]
key = "folder"
label = "Folder"
default = "inbox"
```

| Key | Meaning |
|-----|---------|
| `type` | The event type. Required. |
| `label`, `description` | Shown in the editor; translated through the capa's `i18n/` catalogs. |
| `polled` | `true`: core polls for the event with a poller this capa registers (below). A pushed event (`false`) arrives through `/events` as before. |
| `editor_label` | The trigger editor's tab name (e.g. "On new email"). |
| `config` | The settings a trigger takes; nothing else is accepted in its `event_config`. |

Each `[[plugin.triggers.config]]` setting:

| Key | Meaning |
|-----|---------|
| `key`, `label` | The setting's key in `event_config`, and its translated label. |
| `kind` | `"text"`, the only kind so far. |
| `default` | Used when the trigger names no value. |
| `default_from` | A plain (non-secret) setup field the connection keeps in its env (`env_fields`, or the OAuth setup's delegated identities); its value for the trigger's login is the default. |
| `allowed_from` | A plain setup field holding a list (commas, semicolons, spaces or newlines). When the login's own setup lists anything there, the setting only takes one of those values (ASCII case ignored) — checked on save and again before every poll. |
| `acts_as` | The login acts as this setting's value: a delegating login (a Google service account) gets a token for it. Needs `allowed_from`; for a delegating login an empty list allows nobody. At most one per trigger. |

A polled trigger listens on the organisation login the agent is fixed to for
the capa's tools — a credential, or an OAuth connection of the capa's
provider — and is switched off when that login goes or changes.

### The `event_sources` entry point

A capa with a polled event registers its poller in process:

```toml
[plugin.entry_points]
event_sources = "events.source:register"
```

```python
def register(contrib):
    # credential_type only when the capa's tools sign in with a credential;
    # left out, the trigger listens on an OAuth connection of the provider.
    contrib.add_poller("mail.received", poll, credential_type="mail_account")

async def poll(login_fields, event_config, cursor) -> PollResult: ...
```

The poller contract (`oc8.events.sources`):

- **`cursor=None` means "take a baseline"**: return the current position as
  `next_cursor` and no events — what is there already is not news.
- **At most `MAX_EVENTS_PER_TICK` (20) events** per poll; the cursor stops
  at the last one handed over and the next poll goes on from there.
- **`delivery_id` is the server's own id** for the item (a mail's id in the
  mailbox, never its `Message-ID`, which the sender chooses): core fires each
  `(source, delivery_id, trigger)` once, so polling the same item twice is
  harmless.
- `login_fields` holds the credential's values, or for an OAuth login a fresh
  `access_token` plus `account_label`, `grant_type`, `provider` and
  `allowed_<key>` lists; never write any of it into an error.
- The poll must not block the event loop and must finish within 60 seconds;
  a raised exception is recorded on the trigger and retried with back-off.

## Why the bridge folder is `mcp_bridge/`, never `mcp/`

The obvious first instinct is to name an MCP tool bridge's folder `mcp/` —
but `mcp` is also the name of the installed MCP SDK (`mcp>=1.2,<3`, a hard
backend dependency), and every bridge does `import mcp.types` /
`from mcp.server import Server`. A bridge is launched as a subprocess with
its own capa root on `PYTHONPATH`
(`microsoft365/tool_pack.toml`'s `connections.config.env.PYTHONPATH =
"/app/capas/microsoft365"` is the real, shipped value), and once that
root is on the path, a local package literally named `mcp/` **shadows the
SDK** — the bridge's own `import mcp.types` resolves to itself instead of
the installed package. The failure surfaces as the subprocess dying at
startup with `ModuleNotFoundError: No module named 'mcp.types'`, well
after anyone is looking at folder names. `mcp_bridge/` reads just as
clearly and can never collide with an installed dependency.

## The old flat layout is rejected, not ignored

This was a hard cutover. An inline `[plugin.tool_pack]` table, an inline
`[plugin.setup]` table, or a flat `guardrails.toml` file at a capa's
root is a **hard discovery error** naming exactly where that content
moved to — never a silent skip. A silently-ignored leftover would quietly
strip a capa of its guardrails or its whole setup form; that's worse
than refusing to install it. If you see an error like `"[plugin.tool_pack]
has moved out of plugin.toml into a sibling tool_pack.toml"`, that's the
mechanism working as designed — split the file rather than working around
the error.

## Credentials

Never put a credential in the source config — it's stored as plain JSON on
the `data_source` row. Take one of two routes instead:

- **OAuth** — set `requires_oauth = "google"` (or another registered
  provider) on your `Connector`, and call `await auth.token()`. You get a
  fresh access token; refresh, expiry, and storage are none of your
  business. `gdrive_source` does this.
- **Anything else** — put the *name* of a stored secret in your config
  (e.g. `accessKeyRef`) and call `await auth.secret(name)`. The value
  lives in the tenant's encrypted secret store. `s3_source` does this.

Either way, a leaked `data_source` row leaks a reference, not a key.

## Next

- [Guardrails & Permissions](guardrails-and-permissions.md) for `guardrails/*.toml`.
- [Setup Forms & OAuth](setup-forms-and-oauth.md) for `setup/`.
- [Dependencies & Requirements](dependencies-and-requirements.md) for
  `plugin_depends` and `requirements`.
