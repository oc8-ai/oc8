# Getting started: installing oc8 and connecting to it over MCP

Two separate things the user might need help with before any operation in
this skill can run at all. Neither is something you (an external AI client
talking to oc8 only over MCP) can execute yourself -- you have no shell or
server access to the user's machine. Both are guidance: tell the user what
to run or click, in their own terminal/browser, then wait for them to
confirm before continuing.

## 1. Installing oc8

oc8 is self-hosted only -- there is no oc8.cloud signup. If the user doesn't
have a running instance yet, walk them through this instead of assuming one
exists:

```bash
git clone https://github.com/oc8/oc8.git
cd oc8
./scripts/quickstart.sh   # Windows: .\scripts\quickstart.ps1
```

The script checks for Docker/Podman, creates `.env` from `.env.example` if
missing, and asks which mode to run -- tell the user to pick **Community**
(empty instance, real password setup) unless they specifically want the
seeded **Demo** data. It needs Docker + Compose v2, 4 GB+ free RAM, and Git.
Full walkthrough with first-login/first-agent steps: `docs/user/quickstart.md`
in the repository (or
https://github.com/oc8/oc8/blob/main/docs/user/quickstart.md if you don't
have repo access).

For a production deployment (not a local trial), point the user at
`docs/DEPLOY.md` instead -- it covers the security checklist (rotating
`OC8_JWT_SECRET`/`OC8_SECRET_KEK`, setting `OC8_ENV=prod`, never exposing
dev-login) that `quickstart.sh`'s Community mode does not apply for you.
`docs/user/install-and-maintain/index.md` is the index for upgrades,
backups, and monitoring once the instance is live.

Once `docker compose up -d --build` (or the quickstart script) finishes and
the user has logged in and created their first department/agent through the
oc8 UI, they're ready for step 2.

## 2. Connecting you to it over MCP

This is what `SKILL.md`'s "Connecting" section already walks through in
brief; this is the expanded version for when the user hasn't done it before
and needs each click spelled out.

1. **Get an API key.** In the oc8 UI, tell the user to go to
   **Settings → API keys** and create a new key. It's shown in plaintext
   exactly once, at creation -- tell them to copy it immediately (it looks
   like `oc8_ak_...`) and paste it to you. A key is scoped to exactly the
   permissions of the oc8 member who created it (never more), so if later
   calls 403 with "missing permission", that's their oc8 role, not a bug --
   they need a role change or a key from a member with the right role, not
   a different key format.
2. **Point your MCP client at the gateway.** The URL is
   `https://<their-oc8-host>/mcp/external` (or
   `http://localhost:<port>/mcp/external` for a local quickstart instance,
   using whatever port their `.env` maps the backend to). Authenticate with
   the key as a bearer token: `Authorization: Bearer oc8_ak_...`.
   - **Claude Desktop:** add an entry under `mcpServers` in
     `claude_desktop_config.json` (macOS:
     `~/Library/Application Support/Claude/claude_desktop_config.json`;
     Windows: `%APPDATA%\Claude\claude_desktop_config.json`) pointing at
     that URL with the bearer token in its `headers`. Restart Claude
     Desktop to pick up the change.
   - **claude.ai / Claude web:** add it under Settings → Connectors → Add
     custom connector, same URL and bearer token.
   - **ChatGPT:** create a Custom GPT (or edit an existing one), add an
     Action pointing at the same URL, and set the bearer token as the
     Action's API key auth. `references/chatgpt-instructions.md` in this
     skill is the instructions text to paste into that GPT's configuration
     so it behaves the same way this skill does.
3. **Confirm the connection.** Once configured, calling
   `copilot_list_proposals` with an empty tenant should return an empty
   list rather than an auth error -- that's the cheapest way to confirm the
   key and URL are both right before proposing anything real.

If the user gets a 401/403 at this step, the most common causes are: the
key was copied with a trailing space/newline, the URL is missing the
`/mcp/external` path (pointing at the oc8 app itself instead of the
gateway), or the member who created the key doesn't have the Copilot
permission for what's being attempted -- check the key and role in the oc8
UI before assuming the gateway itself is broken.
