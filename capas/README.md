# Capas

This is the capas directory. Drop a capa **folder** in here, oc8 discovers it,
and it shows up on the **Capas** screen as *available*. Nothing is activated by
being present — an operator still has to press **Install**, per tenant.

The model is Odoo's `addons` directory: putting a folder here **is** the trust
decision. The folder layout itself is also modelled on Odoo modules — a slim
core manifest plus clearly separated subfolders per responsibility, instead of
one large file and a Python package named after the capa.

This file is the flat reference. For a guided walkthrough — a tutorial that
builds a working capa step by step, and the same material split into
focused pages — see [`docs/developer/`](../docs/developer/index.md).

## What a capa folder looks like

```
capas/
  vertrieb_bundle/
    plugin.toml          <- required, and the folder name must match `name`
```

The folder name **is** the capa id and must equal the manifest's `name`. A
mismatch is reported as an error rather than installed, so a renamed folder can't
silently install something else.

### Claude Agent SDK plugin compatibility

Capas are a **superset** of the [Claude Agent SDK plugin standard](https://code.claude.com/docs/de/agent-sdk/plugins).
A standard Claude plugin folder works as a capa without `plugin.toml`; add
`plugin.toml` beside it to layer oc8 governance (trust, permissions, guardrails,
setup, Docker runtimes).

| Layout | `source_format` |
|--------|-----------------|
| `plugin.toml` only | `oc8` |
| Claude layout only (`.claude-plugin/`, `skills/*/SKILL.md`, …) | `claude` |
| Both | `hybrid` |

See the compatibility matrix at the end of this file, and the developer guide [Claude plugin capas](../docs/developer/claude-plugin-capas.md).

## The full folder layout

Every file and subfolder below `plugin.toml` is **optional** — a manifest-only
capa (a department template, a skill) ships just `plugin.toml` and nothing
else. A capa ships only the pieces it actually needs:

```
capas/<name>/
    plugin.toml              # required — the slim core manifest
    tool_pack.toml            # optional — MCP connection wiring
    guardrails/                 # optional — one file per preset/library entry
        read_only.toml
        assist_with_approval.toml
        mail_assistant_drafts_only.toml
        ...
    setup/                        # optional — present only if the capa has a setup form
        fields.toml                # required whenever setup/ exists
        validation.toml             # optional
        oauth_provision.toml         # optional
        mcp.toml                      # optional
    i18n/                           # optional — translation catalogs
        de.po
    connector/                        # optional — a RAG/knowledge connector
        __init__.py
        connector.py
    mcp_bridge/                          # optional — an MCP tool bridge (subprocess)
        __init__.py
        __main__.py
        graph.py                            # (or whatever the capa's own thin API client is called)
        tools/
            mail.py
            calendar.py
            ...
    tests/                                    # the capa's own pure-unit tests
        test_connector.py
        ...
```

Two real, shipped capas are the worked examples for the rest of this
document:

- **`gdrive_source/`** — the simplest capa that ships code: `plugin.toml`
  plus a `connector/` package and a `tests/` folder. Nothing else.
- **`microsoft365/`** — a capa that ships *both* halves at once: a
  `connector/` (SharePoint/OneDrive as a knowledge source) and an
  `mcp_bridge/` (Outlook/Calendar/Teams/Word/Excel/PowerPoint as MCP tools),
  plus `tool_pack.toml`, ten `guardrails/*.toml` files, and a three-file
  `setup/`. `google_workspace/` is the other capa in this shape (its
  `setup/` has all four files, including `validation.toml`).
- **`moodle/`** — a tool-pack capa with its own stdio bridge (`mcp_bridge/`)
  over Moodle's web service REST API: courses, participants, grades,
  submissions, enrolments, messages and forum posts, with read/modify scopes,
  outward marks and five guardrails. One `moodle_token` credential (site URL +
  token) per site.

Two other complete working examples ship in this directory:

- **`vertrieb_bundle/`** — a data-only department bundle. No code, no folders
  beyond `plugin.toml`.
- **`s3_source/`** — an S3 knowledge connector. Ships a `connector/` package
  and reads its credentials from the secret store **by reference**.

## Why the bridge folder is `mcp_bridge/`, not `mcp/`

The obvious first instinct for an MCP tool bridge's folder name is `mcp/` —
but `mcp` is also the name of the installed MCP SDK (`mcp>=1.2,<3`, a hard
backend dependency), and every bridge does `import mcp.types` /
`from mcp.server import Server`. A bridge is launched as a subprocess with its
own capa root on `PYTHONPATH` (`microsoft365/tool_pack.toml`'s
`connections.config.env.PYTHONPATH = "/app/capas/microsoft365"` is the real,
shipped example), and once that root is on the path, a local package literally
named `mcp/` **shadows the SDK** — the bridge's own `import mcp.types` resolves
to itself instead of the installed package. The failure isn't a clean import
error either: it surfaces as the subprocess dying at startup with something
like `ModuleNotFoundError: No module named 'mcp.types'`, well after the point
where anyone is looking at folder names. `mcp_bridge/` reads just as clearly
and can never collide with an installed dependency — use it, always.

## `plugin.toml`

The `[plugin]` table maps 1:1 onto oc8's manifest schema:

```toml
[plugin]
name = "vertrieb_bundle"      # must equal the folder name
version = "1.0.0"             # semver; installing the same version twice is rejected
type = "department_template"  # see the table below
trust = "first_party"         # first_party | verified | community
summary = "One line, shown in the Capas UI"
core_compat = ">=0.1"         # optional PEP 440 specifier against the core version
permissions = []               # a LIST of permission strings
depends = []                   # capabilities that must be present, else install fails
plugin_depends = []            # OTHER CAPAS this one needs installed first — see below
requirements = []              # PEP 508 strings this capa's IN-PROCESS code needs — see below
```

Two things to know:

- **Unknown keys are a hard error.** The schema is strict, so a typo fails loudly
  instead of being silently ignored. If a field isn't listed here or in
  `backend/src/oc8/capas/manifest.py`, it doesn't exist.
- **`permissions` is a list**, not a table.

`plugin.toml` itself carries only the core identity fields plus whichever of
`entry_points`, `handles`, `sandbox`, `department_template`, `skill_template`,
`skill_pack`, `flow_template`, `config`, `tools`, `mcp`, `skills`, `triggers`,
`policy`, `io`, `phases`, `hooks` the capa's `type` actually uses. Everything
else — the MCP connection, the guardrails, the setup form — lives in its own
file or folder, described next.

## The manifest split

The manifest is still one pydantic `Manifest` object once discovery has
assembled it — nothing downstream (`materialise.py`, `configure_plugin`,
`outward.py`) changed. What changed is that a capa no longer has to cram
everything into one `plugin.toml`; discovery opportunistically reads whichever
of the files below are present and merges them into the same fields it always
populated.

### `tool_pack.toml`

If a capa exposes MCP tools, its connection wiring lives in a sibling
`tool_pack.toml`. **This file's own top level *is* the `tool_pack` table** —
there is no `[plugin.tool_pack]` wrapper to nest under. `microsoft365`'s real
file:

```toml
[[connections]]
key = "primary"
name = "microsoft365"
transport = "stdio"
server_url = ""

[connections.scopes]
read = ["mail_search", "mail_get", ...]
send = ["mail_send", "mail_reply", ...]

[connections.config]
command = "python"
args = ["-m", "mcp_bridge"]
outward_tools = ["mail_send", "mail_reply"]

[connections.config.env]
PYTHONPATH = "/app/capas/microsoft365"

[connections.config.secret_env]
GRAPH_ACCESS_TOKEN = "oauth:graph_access_token"
```

### `guardrails/*.toml`

One file per guardrail — whether a generic preset (`read_only`,
`assist_with_approval`, `autonomous_with_limit`, `internal_only`,
`no_deletions`) or a curated use-case library entry
(`mail_assistant_drafts_only`, `cross_never_deletes`, …) — sitting in the same
flat `guardrails/` folder.

These are genuinely **two different pydantic models**, and every file
declares which one it is with a required discriminator field:

- **`kind = "preset"`** — a `GuardrailPreset`: one of a capa's fixed generic
  ceilings (`recommended: bool`, no `use_case`), attached to a specific
  `tool_pack.toml` connection. `microsoft365/guardrails/read_only.toml`:

  ```toml
  kind = "preset"
  connection = "primary"
  key = "read_only"
  label = "Read only"
  summary = "Can read and search, but change nothing."
  recommended = false
  read = true
  write = false
  send = false
  approval_actions = []
  approval_eur = ""
  ```

  `connection` names which `tool_pack.toml` connection the preset belongs to.
  **When omitted, the preset binds to the capa's sole connection** —
  whatever that connection's own `key` is, never the literal string
  `"primary"`. Only name `connection` explicitly when a capa declares two or
  more connections; every capa shipped today uses `key = "primary"` so this
  distinction has never actually mattered yet, but hard-coding `"primary"`
  would give a community author writing their first single-connection capa
  a bogus "unknown tool_pack connection" error.

- **`kind = "library"`** — a `Guardrail`: a documented, named use-case
  scenario (`use_case: str`, required; an `adjustable` list of tunable
  numeric parameters), attached to the capa as a whole rather than to one
  connection. `odoo_mcp/guardrails/cross_never_deletes.toml` (one of its 28
  curated entries):

  ```toml
  kind = "library"
  key = "cross_never_deletes"
  label = "Never delete, across every area"
  summary = "Creates, updates and sends records, but delete_record is never available ..."
  use_case = "cross_cutting"
  read = true
  write = false
  send = true
  approval_eur = ""
  approval_actions = []
  only = ["search_records", "get_record", "list_models", ...]
  ```

**The filename (minus `.toml`) must equal the guardrail's own `key` field**,
regardless of `kind` — the same "folder name must equal manifest `name`"
consistency check applied one level deeper. A mismatch is a hard discovery
error, not a silent rename.

### `setup/`

Present only if the capa has a setup form. Four fixed filenames — discovery
reads exactly these names, not "every `.toml` in the folder", because each
plays a distinct, non-repeating role (unlike `guardrails/`, which is a folder
of *N* same-shaped entries):

- **`fields.toml`** — **required whenever `setup/` exists.** Carries `title`,
  `description`, `submit_label`, the field list, and
  **`validate_entry_point`** — the dotted `module:attr` path to the capa's
  own credential-validation hook. It lives here and nowhere else.
  `telegram_approvals/setup/fields.toml`:

  ```toml
  title = "Telegram verbinden"
  description = "Der Bot-Token von @BotFather. Verlässt den Secret Store nie unverschlüsselt."
  submit_label = "Speichern & testen"
  validate_entry_point = "channel.setup:validate"

  [[fields]]
  key = "bot_token"
  label = "Bot-Token"
  kind = "password"
  required = true
  secret_ref = "telegram/bot_token"
  ```

  Note the dotted path: `channel.setup:validate`, naming the capa's *new*
  package (`channel/`), **not** `telegram_approvals.setup:validate`. Every
  entry point anywhere in a capa's manifest — `entry_points`,
  `validate_entry_point`, anything else dotted — points at the new,
  responsibility-named folder, never the old capa-named one.

- **`validation.toml`** — optional. The `any_of` validation rule, if the form
  needs "at least one of these fields must be filled in."
- **`oauth_provision.toml`** — optional. The `SetupOAuthProvision` block, if
  submitting the form also provisions an OAuth connection.
- **`mcp.toml`** — optional. The `SetupMcpSpec` block (connection key, launch
  command, env/secret_env field mappings, department field), if setup also
  wires an MCP connection.

`microsoft365` ships three of the four (no `validation.toml`);
`google_workspace` ships all four.

## The old flat layout is rejected, not ignored

This was a hard cutover, not a migration period with two supported shapes. An
inline `[plugin.tool_pack]` table, an inline `[plugin.setup]` table, or a flat
`guardrails.toml` file still sitting at a capa's root is a **hard discovery
error**, naming exactly where that content has moved to — never a silent skip.
A silently-ignored leftover would quietly strip a capa of its guardrails or
its whole setup form; that is worse than refusing to install it. If you're
retrofitting a capa (or copying an old example from memory) and see an error
like "`[plugin.tool_pack]` has moved out of `plugin.toml` into a sibling
`tool_pack.toml`" or "`guardrails.toml` has moved to `guardrails/<key>.toml`",
that is the mechanism working as designed — split the file, don't try to keep
the old one around "just in case."

## Capa types — what actually works today

Be aware before you build one:

| `type` | Status | Needs code? |
|---|---|---|
| `department_template` | **Works.** Install, then instantiate into a real department with its team of agents. | no |
| `agent_template` | **Works thinly.** Instantiates a single agent. | no |
| `skill` | **Works.** On enable, the shipped skill becomes a real Skill in your library. | no |
| `flow_template` | **Works.** On enable, becomes a Flow + version. | no |
| `tool_pack` | **Works.** On enable, becomes MCP connections — created *disconnected*, so an operator still has to test them. | no |
| `connector` | **Works.** Contributes a knowledge connector. | **yes** |
| `runtime_adapter` | **Works.** Contributes an agent runtime implementation. | **yes** |
| `model_adapter` | **Works.** Contributes an LLM provider. A tenant can only configure a model against a provider it enabled. | **yes** |
| `approval_channel` | **Works.** Contributes an approval channel (Telegram, WhatsApp, ...). | **yes** |
| `core_extension` | **Works for trusted capas.** Contributes in-process hook handlers. A `community` capa still takes the sandboxed path, whose worker is a stub — so it registers but cannot execute (that needs §8.5). | **yes** |

The data-only types (`skill`, `flow_template`, `tool_pack`, and the two
templates) materialise when the capa is **enabled** — that is where consent
happens, and they have nothing to name or place. Materialisation is idempotent,
and **disabling a capa does not delete what it created**: toggling a capa
off must not destroy work built on top of it.

Note that `type` does not gate what a capa's `entry_points` may contribute:
`microsoft365` and `google_workspace` both declare `type = "tool_pack"` while
*also* shipping a `connectors` entry point — a tool-pack capa can ship a
knowledge connector too, in the same folder, at the same time. Neither the
connector registry nor tool-pack materialisation reads `type` to decide
whether to accept a contribution.

## Capas that ship code

A `connector`, `mcp_bridge`, `runtime_adapter`, `model_adapter`,
`approval_channel` or `core_extension` capa adds Python code alongside its
manifest, in a folder named for its **responsibility**, not for the capa
itself:

| Capa `type` | Code folder |
|---|---|
| `connector` | `connector/` |
| `runtime_adapter` | `runtime/` |
| `model_adapter` | `provider/` |
| `approval_channel` | `channel/` |
| MCP tool bridge (any `type`) | `mcp_bridge/` |

This is the direct analogue of Odoo's `models/`/`controllers/` — it makes the
layout instantly legible across every capa without reading its manifest
first, and it is why `gdrive_source/connector/connector.py` and
`s3_source/connector/connector.py` are two entirely different files that
happen to sit at the same relative path. `gdrive_source/plugin.toml`:

```toml
[plugin.entry_points]
connectors = "connector.connector:register"
```

A capa whose folder is named `runtime/` declares its entry point the same
way, just with the folder name (and usually the key) swapped:

```toml
[plugin.entry_points]
runtime = "runtime.runtime:register"
```

Every declared entry point is imported and called with the same contribution
object, so one capa can contribute more than one thing. The key name
(`connectors`, `runtime`, `model_providers`, `channels`, …) is documentation —
what you may actually add is whatever `PluginContributions` exposes:
`add_connector`, `add_runtime`, `add_model_provider`, `add_hook`.

A hook handler additionally has to be **declared** in the manifest:

```toml
[[plugin.handles]]
point = "task.before_create"
priority = 10
```

Declared *and* offered, or it does not run — otherwise a capa could hook a
point its consent screen never showed.

`register(contrib)` is called once per process and contributes implementations:

```python
def register(contrib):
    contrib.add_connector(MyConnector())
```

A `Connector` must satisfy the protocol in
`backend/src/oc8/knowledge/connectors/base.py` (`type_id`, `config_schema`,
`requires_oauth`, and `validate` / `discover` / `fetch`).

### Logger names are pinned, never `__name__`

`getLogger(__name__)` used to be a safe default because each capa's package
had its own unique name. It no longer is: because the code folder is now the
generic, responsibility-named folder (`runtime/`, `connector/`, `channel/`),
`__name__` collapses every capa of the same type onto one logging namespace
— all four `runtime_adapter` capas would log under `runtime.*`, both
approval channels under `channel.channel`, silently destroying per-capa log
attribution the moment a second capa of that type exists. Pin an explicit,
unique name instead:

```python
logger = logging.getLogger("oc8.plugin.<plugin_id>.<module>")
```

Real shipped examples:
`logging.getLogger("oc8.plugin.microsoft365.connector")`,
`logging.getLogger("oc8.plugin.telegram_approvals.channel")`,
`logging.getLogger("oc8.plugin.nanoclaw_runtime.runtime")`. A capa author
copying an existing file for a new capa of the same type will otherwise
reintroduce this bug on day one — the folder name gives no warning that
`__name__` is now the wrong choice.

### Credentials

Never put a credential in the source config — it is stored as plain JSON on the
`data_source` row. Instead, take one of the two routes the `AuthContext` offers:

- **OAuth** — set `requires_oauth = "google"` (or another registered provider)
  and call `await auth.token()`. You get a fresh access token; refresh, expiry
  and storage are none of your business. `gdrive_source` does this.
- **Anything else** — put the *name* of a stored secret in your config
  (`accessKeyRef`, say) and call `await auth.secret(name)`. The value lives in
  the tenant's encrypted secret store. `s3_source` does this.

Either way a leaked `data_source` row leaks a bucket name, not a key.

Four rules govern code capas, and none of them are negotiable:

- **Only trusted capas are imported.** `trust` must be `first_party` or
  `verified`. A `community` capa is never imported at all — running untrusted
  code needs sandbox isolation that does not exist yet. Putting a folder here
  **is** the trust decision, as in Odoo.
- **Installing is not enough — the capa must be enabled.** Enabling is where
  its declared `permissions` are granted. An installed-but-not-enabled capa
  contributes nothing.
- **Contributions are per tenant.** The import is process-wide, but only the
  tenants that enabled the capa can resolve its connector. Another tenant on
  the same installation cannot use it, or even see that it exists.
- **A capa that fails to import is quarantined,** logged, and not retried. It
  does not take the server down, but it also does not silently half-work.

**No hot reload.** A newly dropped folder is discovered immediately, but its code
is imported once per process — changing the code needs a restart.

**No cross-capa import collisions.** Because `connector/`, `runtime/`,
`provider/`, `channel/` and `mcp_bridge/` are shared folder names across every
capa of a given shape, the loader evicts every `sys.modules` entry it
introduces after each capa's entry-point import completes (success or
failure) and removes the capa's `sys.path` entry — so a second capa
importing its own, differently-located `connector` package always gets its
own code, never a sibling's cached module.

## How a capa's own tests work

Pure-unit tests — no database, no FastAPI app, importing capa code directly
— live inside the capa, at `capas/<name>/tests/`, and are collected as
part of the normal `cd backend && uv run pytest` run. Tests that need oc8's
app/DB fixtures (`app_session`, `AppSessionFactory`, and the rest of
`backend/tests/conftest.py`'s chain — those fixtures apply only to tests
collected under `backend/tests/`) stay where they always lived, at
`backend/tests/plugins/<name>/`.

### The settings that make a capa-local `tests/` folder collectable at all

`backend/pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests", "../capas"]
addopts = "--import-mode=importlib"
consider_namespace_packages = true
```

Under pytest's *default* `prepend` import mode, a capa-local `tests/`
folder cannot be collected at all, two independent ways: with a
`tests/__init__.py` per capa, the top-level package name `tests` is already
owned by `backend/tests/__init__.py` and collection dies with
`ModuleNotFoundError: No module named 'tests.test_connector'`; without one,
two capas' identically-named test files collide with `import file mismatch`
(`microsoft365` and `google_workspace` share several test basenames).
`--import-mode=importlib` imports each test file under a path-derived name
instead of a `sys.path`-derived one, which fixes both; `consider_namespace_packages`
keeps `backend/tests`' own `from tests.conftest import …` style imports
resolving. `testpaths` adds `../capas` as a bare directory glob — pytest
still only ever collects files matching `test_*.py`, so a capa's own source
folders are never mistaken for tests.

**A plain `cd backend && uv run pytest ../capas/<name>/tests` run from
outside `backend/`'s own directory does *not* load this config** — pytest
derives its rootdir/inifile from the args, and an arg living outside
`backend/` walks up to the repo root, which has no `pyproject.toml` at all.
The session header then prints no `configfile:` line — that missing line is
the tell. Run capa-local tests as:

```bash
cd backend && uv run pytest -c pyproject.toml ../capas/<name>/tests -v
```

or include `backend/tests` in the same invocation's args.

### The `_plugin_path` fixture

`connector/`, `runtime/`, `provider/`, `channel/` and `mcp_bridge/` are
generic names every capa of that shape reuses, and `sys.modules` is keyed
by **name**, not by filesystem path — so without help, whichever capa's
module lands in the cache first wins for the whole pytest session, and a
second capa's test would silently import the first capa's code. The fix
is a **function-scoped, `autouse=True`** fixture that inserts the capa's own
root onto `sys.path` and evicts the cached module **on both fixture setup and
teardown** — real, shipped example
(`capas/gdrive_source/tests/test_connector_registers.py`):

```python
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop any cached `connector`/`connector.*` module from sys.modules."""
    for _stale in [n for n in sys.modules if n == "connector" or n.startswith("connector.")]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    sys.path.insert(0, str(PLUGIN_ROOT))
    _evict()
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


def test_register_contributes_the_drive_connector() -> None:
    # Function-local, INSIDE the fixture's window.
    from connector.connector import register

    contrib = PluginContributions(plugin_id="gdrive_source")
    register(contrib)
    assert "gdrive" in contrib.connectors
```

For a file at `capas/<name>/tests/test_*.py`, `PLUGIN_ROOT` is
`parents[1]`; for a file at `backend/tests/plugins/<name>/test_*.py` it's
`parents[4]` (`[0]`=`<name>`, `[1]`=`plugins`, `[2]`=`tests`, `[3]`=`backend`,
`[4]`=repo root).

**Why function-scoped, not a module-top prelude:** a module-top prelude runs
at *collection* time. Most of this repo's capa tests import their capa
*inside the test body*, which resolves at *execution* time, long after
collection has finished — a module-top prelude silently does nothing for
those files. A file whose capa imports are genuinely module-level keeps a
plain `sys.path.insert` + eviction prelude *as well* — the two compose, they
are not alternatives to pick between.

**Why the eviction has to be symmetric (setup *and* teardown):** the
production loader (`loader.py`) only evicts the `sys.modules` entries it
itself introduces during its own import call. If a generic name like
`connector` is still cached from a prior test's teardown, `importlib.import_module`
returns that cached module, the loader's own "introduced" set is empty, and
it silently hands back the *wrong* capa's `register` to whatever called it
next. Setup-only eviction is invisible until a second capa shipping the
same folder name gets its own capa-local tests — evict on both ends, always.

A capa with **two** generic packages (`microsoft365`: `connector` +
`mcp_bridge`) evicts both names in the same fixture, not two separate
fixtures.

**One deliberate variant:** in a large, otherwise capa-free test file where
only one test imports a capa's package inside its body, drop `autouse=True`
and have that one test request `_plugin_path` by name as a parameter instead
— autouse would otherwise evict and re-import the capa once per test in a
file that has nothing else to do with it.

**This fixture does not validate `plugin.toml`.** It imports
`connector.connector` directly and never reads the manifest — a typo'd entry
point (`conector.connector:register`) would not be caught by it alone. Every
capa also needs *some* test that asserts the entry-point literal or drives
the real loader through `find_plugin`/`load_plugin` — for `gdrive_source` that
is `backend/tests/plugins/test_gdrive_plugin.py`'s
`test_the_plugin_manifest_declares_a_connector_entry_point` plus its other
tests exercising `find_plugin` end to end.

### Type-checking

A scoped, per-capa `mypy` invocation — never `mypy_path`, and never more
than one capa sharing a folder name (`runtime/`, say) in the same
invocation, or mypy reports "Duplicate module named 'runtime'":

```bash
cd backend && uv run mypy ../capas/<name>/<code-folder> ../capas/<name>/tests
```

Real example: `cd backend && uv run mypy ../capas/gdrive_source/connector ../capas/gdrive_source/tests`
→ `Success: no issues found in 3 source files`. A runtime capa that imports
the shared `cli_harness` helper library names it explicitly, since `mypy_path`
no longer resolves capa code:
`cd backend && uv run mypy ../capas/claude_code_runtime/runtime ../capas/cli_harness/cli_harness`.

**`cd backend &&` is mandatory before every `ruff`/`ruff format`/`mypy`
command scoped to a capa.** `capas/` has no ruff/mypy config of its own
anywhere above it, so running from the repo root instead of `backend/` loses
`backend/pyproject.toml`'s config (its `RUF012` ignore, its `src` classifier
that keeps `oc8` recognised as first-party) and produces spurious lint errors
that look like real ones.

## i18n

```
capas/<name>/i18n/
    de.po
```

Standard gettext catalogs — the same mechanism Odoo itself uses for module
translations. `msgid` is the literal English string exactly as it appears in
`plugin.toml`, `guardrails/*.toml`, `setup/fields.toml` or
`personal_settings.label` — not an invented dotted key:

```po
msgid "Read only"
msgstr "Nur lesen"
```

An author writes normal English literal text in their TOML files with no new
syntax; adding a translation is purely additive, a `de.po` entry alongside it.
There is no English catalog — `label`/`summary`/etc. in the TOML *are* the
English source text, so an `en.po` would just duplicate them.

**Wired at the presentation layer.** `oc8.capas.i18n.translations_for(i18n,
source_text)` resolves one string against every locale a capa's `i18n/`
folder ships, returning `{locale: translated_text}`. `api/v1/mcp.py`,
`api/v1/capas.py` and `api/v1/credentials.py` call it for every
capa-authored string they expose — guardrail/preset `label`/`summary`, a
`GuardrailAdjustable`'s `label`, a capa's `plugin.toml` summary,
`personal_settings.label`, every `setup/fields.toml` field, and a
`credential_types/*.toml` entry's `display_name` plus its own fields'
`label`/`help`/`placeholder` — bulk-resolving all locales into a
`<field>Translations: dict[str, str]` map on the DTO rather than picking one
server-side. That lets the frontend switch language client-side (see
`frontend/src/lib/i18n.tsx`'s `resolveTranslation`) without a refetch. A
string absent from `i18n/<locale>.po` (or a capa that ships no `i18n/` at
all) just falls back to the English `label`/`summary` — there is no hard
requirement to translate everything.

## `plugin_depends` — capa-to-capa dependencies

```toml
[plugin]
plugin_depends = ["other_capa_name"]
```

Names *other capas* (by their `name`, matching their folder name) that must
be installed for this tenant before this one can be. This is a **new, separate
field** from the existing `depends` — `depends` is a list of abstract
*capability* strings the tenant's install must already satisfy, install-time
checked but never auto-resolved; `plugin_depends` names concrete capas and
is auto-resolved.

- **Auto-installs, never auto-enables.** Installing capa `P` recursively
  installs any not-yet-installed entry in `P.plugin_depends` first. The
  dependency ends up *installed but disabled* — exactly as if an operator had
  installed it manually and not yet clicked Enable. Enabling is still a
  separate, explicit decision for every capa, including auto-installed
  dependencies: auto-enabling would silently bypass the consent gate for
  something the operator never directly asked to activate.
- **A missing dependency is a clear install-time error**, never a silent
  skip — a `plugin_depends` entry naming a capa absent from disk fails
  loudly.
- **Cycles are caught at discovery time**, not install time: discovery builds
  the full `plugin_depends` graph across every discovered capa once, and a
  cycle marks every capa in it invalid with a clear error — the same
  "fail loudly and early" principle as the guardrail filename/key check.
- **No version-constraint syntax.** Entries are bare capa names,
  presence-only, matching Odoo's own `depends` simplicity. A capa needing a
  *specific version* of a dependency is a problem for a later design.

No shipped capa uses `plugin_depends` today, but unlike `i18n/` it is fully
wired end to end and directly, thoroughly tested (auto-install-but-not-enable,
cycle detection, missing-dependency error), waiting for the first capa — community-authored,
or a future first-party one — with a genuine cross-capa dependency.

## Per-capa `requirements`

Two different mechanisms, because the two kinds of capa code have
fundamentally different isolation properties — this distinction matters and
is worth understanding correctly before writing your first capa, because
getting it backwards produces two different flavours of silent failure.

### MCP tool-bridge (subprocess) capas — real isolation

An MCP bridge is *already* launched as a separate subprocess, so it can get
genuinely isolated dependencies without touching the core process's own
Python environment at all. Declared in `tool_pack.toml`:

```toml
[connections.config]
requirements = ["httpx>=0.27", "some-vendor-sdk>=2.0"]
```

Core's launch code (`backend/src/oc8/agent/mcp_requirements.py`) wraps the
configured `command`/`args` in `uv run --with <requirements> -- <command>
<args…>`, which resolves and caches an ephemeral dependency overlay per
invocation — nothing is installed into the shared core environment, and if
core ever changes *how* it achieves that isolation, no capa manifest needs
to change. This is genuine, per-launch isolation: two bridges can declare
conflicting requirement versions and neither affects the other, or the core
process.

### In-process connector capas — diagnostic only, not installed

A connector's code is `importlib.import_module`'d directly into the *same*
Python interpreter core itself runs in — genuine per-capa isolation there
would mean a separate interpreter/venv per connector, well beyond this
restructure's scope. Declared in `plugin.toml`'s core section instead:

```toml
requirements = ["python-docx>=1.1", "openpyxl>=3.1"]
```

`loader.py` checks these immediately before importing the capa's entry
point — for each declared requirement, is it importable (via
`importlib.metadata` + `packaging.requirements`) in the *current* backend
environment? If anything is missing, the capa refuses to load with an error
naming exactly which package(s) are absent and that they need to be added to
`backend/pyproject.toml` — an operator/core-maintainer action, not something
the capa or its author can do automatically. **This is explicitly
diagnostic, not installation**: declaring `requirements` here does not make
anything get installed. It exists to replace an opaque `ModuleNotFoundError`
deep inside an import traceback with a specific, named diagnosis at load
time — closing exactly the kind of gap that once caused a real, shipped
regression (an unrelated edit to `backend/pyproject.toml` silently broke the
Microsoft 365 capa's document-extraction dependencies, undetected until a
full test run, because nothing had declared "this capa needs these three
packages" as a checkable fact).

No shipped capa declares `requirements` today for either path — both exist
and are directly tested, waiting for the first capa that actually needs
them.

## Installing

Unchanged by this restructure — the manifest split only changes how
`discovery.py` assembles a `Manifest` from disk, not how installing or
configuring the capas path works.

1. Put the folder in the capas path.
2. Open **Capas** in the UI → the capa appears under *available*.
3. Press **Install**. That installs it **for your tenant only**.

Or via the API:

```
GET  /api/v1/capas/available
POST /api/v1/capas/install-from-disk   {"pluginId": "vertrieb_bundle"}
```

Installing requires the `org_admin` role. The request names a capa **id**, never
a path — the capas path is operator configuration, so nobody can install
something the operator hasn't placed on disk.

A capa that ships code needs one more step: **enable** it, which grants its
declared permissions. Data-only bundles work as soon as they are installed.

Discovery is **installation-wide** (every tenant sees the same folders);
installation is **per tenant** (one tenant installing does not install it for
anyone else). If a capa declares `plugin_depends`, installing it also
installs (but does not enable) any not-yet-installed dependency first.

## Configuring the path

Unchanged by this restructure. `OC8_CAPAS_PATH`, default `capas`,
comma-separated for several roots. When the same capa id appears in more
than one root, the **first** root wins and the later one is shadowed.

Note the default is relative to the process working directory. In Docker that is
`/app`, and `docker-compose.yml` mounts this directory to `/app/capas` read-only.
Running the backend locally from `backend/`, set `OC8_CAPAS_PATH=../capas`
(or an absolute path) — otherwise it looks for `backend/capas` and finds nothing.

## Changing a capa

Discovery re-scans on every request, so editing a `plugin.toml` (or any of its
sibling `tool_pack.toml`/`guardrails/`/`setup/`/`i18n/` files) shows up
immediately in the *available* list. An already-installed capa is a database
snapshot of the manifest at install time — bump `version` and install again to
pick up changes.

## Claude plugin compatibility matrix

| Claude component | oc8 v1 | oc8-only extension |
|------------------|--------|--------------------|
| `skills/*/SKILL.md` | Yes | `skills/*.toml` in parallel |
| `agents/*.md` | Yes | `[plugin.agent_template]` in `plugin.toml` |
| `.mcp.json` | Yes | `tool_pack.toml` + `guardrails/` |
| `hooks/hooks.json` | Yes (runtime bridge) | `[plugin.handles]` + Python hooks |
| `commands/` | Yes (imported as skills) | — |
| `output-styles`, `themes`, `monitors`, `lsp`, `bin/` | No (discovery warning) | — |
| `guardrails/`, `setup/`, `runtime/` | — | Yes |

Claude hook actions require consent: `hooks:claude:command`, `hooks:claude:http`,
`hooks:claude:mcp` (derived at discovery when not declared in `plugin.toml`).

Full guide: [docs/developer/claude-plugin-capas.md](../docs/developer/claude-plugin-capas.md).
