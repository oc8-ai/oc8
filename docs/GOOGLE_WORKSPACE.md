# Connecting Google Workspace

This is the setup guide the capa's own form points at. It walks through the
Google Cloud side once — creating a service account, sharing a Shared Drive
with it, and (only if you want Gmail/Calendar) authorizing domain-wide
delegation — and then through the five things oc8's form asks for.

You need to be able to create a service account in a Google Cloud project tied
to your Workspace domain, and, for the Gmail/Calendar step only, you need
**Super Admin** access to the Google Workspace Admin Console. If you only want
the Shared Drive knowledge base and Drive/Docs/Sheets/Slides tools, you can
skip every admin-console step below entirely.

Budget about 15 minutes without Gmail/Calendar, 25 with.

---

## What kind of connection this is

oc8 connects to Google Workspace as a **service account** — a Google identity
that belongs to no person and does not sign in through a browser. Two
consequences worth knowing up front:

- **The service account has its own identity, separate from any mailbox.**
  That identity — call it the *self identity* — is what Shared Drive access
  and Docs/Sheets/Slides tools use. It is proven with a JWT signed by the
  service account's own private key, requesting only the
  `https://www.googleapis.com/auth/drive` scope. That single `drive` scope is
  genuinely sufficient for the Drive, Docs, Sheets and Slides APIs — it is
  **not** a gap that the form does not separately ask for
  `documents`/`spreadsheets`/`presentations` scopes; the self identity never
  requests them because it does not need to.
- **Gmail and Calendar need a second, different kind of proof: domain-wide
  delegation.** A service account cannot open a Gmail inbox as itself — Gmail
  and Calendar only make sense for a real mailbox. Domain-wide delegation lets
  your Workspace admin authorize this service account, once, to *impersonate*
  specific mailboxes for a fixed set of scopes. Each mailbox you list in the
  form gets its own token, minted fresh per use and never shared with the
  self identity's token.

If you only need the knowledge base and Drive/Docs/Sheets/Slides tools, skip
straight to [step 3](#3-create-the-service-account) — domain-wide delegation
(steps 4–5) is entirely optional and can be added later without redoing
anything.

---

## 1. Create or choose a Google Cloud project

1. Go to [Google Cloud Console](https://console.cloud.google.com) and either select an existing
   project or create a new one (top-left project picker → **New Project**).
   The project must belong to your Workspace organization.
2. Left menu → **APIs & Services** → **Library**, and enable each API you plan
   to use:

   | You want | Enable |
   |---|---|
   | Shared Drive knowledge base, Drive tools | Google Drive API |
   | Gmail tools | Gmail API |
   | Calendar tools | Google Calendar API |
   | Docs tools | Google Docs API |
   | Sheets tools | Google Sheets API |
   | Slides tools | Google Slides API |

   Enabling an API you do not end up using is harmless; the reverse — a tool
   call against a disabled API — fails clearly with a 403 naming the API.

---

## 2. Create the service account

1. Left menu → **IAM & Admin** → **Service Accounts** → **Create Service
   Account**.
2. Give it a name you will recognise later — `oc8` is fine. Click **Create and
   Continue**, then **Done** (no project-level IAM role is needed — every
   permission this capa uses comes from Shared Drive membership or
   domain-wide delegation, not from Google Cloud IAM).
3. Click into the new service account, note its **email address** (looks like
   `oc8@your-project.iam.gserviceaccount.com`) — you will need it in step 3.
4. **Keys** tab → **Add Key** → **Create new key** → **JSON** → **Create**.

A `.json` file downloads immediately and is shown **once**. Open it in a text
editor — its full contents (not just one field) is what the oc8 form's
**Service account key (JSON)** field wants. Keep the file somewhere safe until
you have pasted it in; delete your local copy afterward.

> Rotating a leaked or expiring key later is safe: create a new key in this
> same service account, paste its JSON into the same oc8 setup form, submit.
> oc8 updates the existing connection in place (matched by the service
> account's email address) rather than creating a second one.

---

## 3. Share a Shared Drive with the service account

Skip this section if you only want Gmail/Calendar tools with no knowledge base
or Drive/Docs/Sheets/Slides access.

1. In Google Drive, open (or create) a **Shared Drive** — not a personal "My
   Drive" folder; the connector only reads Shared Drives.
2. **Manage members** → add the service account's **email address** from step
   2 → give it at least **Content manager** (Viewer is enough for read-only
   tools; Content manager is needed for upload/update/delete tools).
3. Get the Shared Drive's ID from its URL:
   `https://drive.google.com/drive/folders/`**`0AbCdEfGhIjKlUk9PVA`** — that
   trailing segment is the Shared Drive ID.

Several Shared Drives go in the same form field, comma-separated. Emptying
this field later and submitting again **stops** the knowledge sync and turns
off the Drive/Docs/Sheets/Slides tools without deleting anything already
indexed.

---

## 4. Authorize domain-wide delegation (only for Gmail/Calendar)

Skip this section entirely if you only want the Shared Drive knowledge base
and Drive/Docs/Sheets/Slides tools — the **Mailboxes to act as** field may be
left empty, and you can come back and fill it in later without redoing
anything above.

1. Go to [Google Admin](https://admin.google.com) (requires Super Admin) → **Security** →
   **Access and data control** → **API controls** → **Domain-wide
   delegation**.
2. Click **Add new**.
3. **Client ID**: the service account's **numeric** client ID — found on the
   service account's detail page in Google Cloud Console (**not** the email
   address, and **not** the JSON key file's `client_id`-looking `project_id`
   — it is the long all-digits **Unique ID** shown under the service
   account's name).
4. **OAuth scopes**: paste exactly this, comma-separated, with no extra
   spaces:

   ```
   https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/calendar
   ```

   This must match byte-for-byte (aside from the comma Google's own form
   asks for here). oc8's own scope-exactness check compares against the
   *space*-separated form of this same pair — a mismatch here means a setup
   that looks fully configured on both sides still fails every Gmail/Calendar
   tool call with "domain-wide delegation is not authorized."
5. Click **Authorize**.

---

## 5. Fill in the oc8 form

Capas → **Google Workspace** → *Connect Google Workspace*.

| Field | What to paste |
|---|---|
| **Service account key (JSON)** | the full contents of the `.json` file from step 2 |
| **Shared Drive IDs to index** | comma-separated IDs from step 3, or leave empty |
| **Department** | which department's agents get these tools |
| **Mailboxes to act as for Gmail/Calendar** | comma-separated addresses you authorized in step 4, or leave empty |
| **Default mailbox for Gmail/Calendar tools** | one of the mailboxes above — optional, but usually wanted |

**At least one of Shared Drive IDs or Mailboxes must be filled in.** A
submission with both empty proves nothing was actually configured and is
rejected outright, rather than succeeding with 31 tools that would all fail on
first use.

**Department is not decoration.** Agents are given tools by department. A
connection with no department set is reachable by nobody — setup succeeds,
the credential tests green, and every agent run has zero Google Workspace
tools with no error anywhere. Setting up the same capa for a second
department creates a second connection; it does not move the first one.

**Default mailbox must be one of the mailboxes you listed.** A typo here is
rejected immediately at setup time with a clear error naming the mismatch —
not discovered later, deep inside a failed Gmail tool call.

Submitting the form checks the credentials against Google before saving
anything — the service account can mint a token for a Shared Drive it was
never added to, and Drive only reveals that mismatch when the specific drive
is queried, not when the token is minted, so this proves Shared Drive
membership too, one drive at a time.

---

## Which mailbox do Gmail/Calendar tools act on?

Because a service account has no inbox of its own, every Gmail/Calendar tool
needs to know which mailbox it is acting as. There are two ways it can find
out, and they work together:

- **Default mailbox** (this form, optional). Every Gmail/Calendar action uses
  this mailbox unless the agent explicitly names another. This is what most
  tenants want: set it once, never think about it again.
- **`mailbox`** (per tool call, optional). Any tool call may name a different
  mailbox — provided it is one of the addresses authorized in step 4 — which
  is what lets one agent work across several mailboxes.

If you set no default mailbox **and** an agent makes a Gmail/Calendar call
without naming one, that call fails with a clear "no mailbox given and no
default_mailbox configured for this connection" error rather than doing
something to an unexpected mailbox.

Drive, Docs, Sheets and Slides tools are unaffected by any of this — they
always act as the service account's own self identity, never as a delegated
mailbox.

---

## The "On new email" trigger

An agent fixed to this connection for its tools can be started by new mail:
in the agent's trigger editor, the **On new email** tab watches one Gmail
label and starts a run for each new mail, with the mail handed over as data.
oc8 checks every minute (`OC8_EVENT_POLL_SECONDS`), minting a token for the
watched mailbox through the same domain-wide delegation as the tools.

- **Mailbox** defaults to the default mailbox above; **Label** to `INBOX`
  (a label ID such as `INBOX` or `Label_12`).
- **Only authorized mailboxes.** The mailbox must be one of the delegated
  mailboxes in this form (`delegated_mailboxes`). oc8 refuses any other when
  the trigger is saved and checks again before every poll, so removing an
  address from the list stops its trigger from reading it. With an empty list,
  a service account cannot watch any mailbox.
- **A signed-in account** (instead of a service account) watches only its own
  mailbox.
- If the connection is removed or replaced, the trigger switches itself off;
  switch it on again once the agent has its new connection.

---

## Checking it really works

The form's own submission proves the credential, Shared Drive membership (if
configured) and delegated access to the first listed mailbox (if configured).
To satisfy yourself the rest works, mint a token from the service account's
key and run the calls below — they cover every shape this capa uses.

| Check | Call | Proves |
|---|---|---|
| Shared Drive | `GET /drive/v3/drives/{driveId}?supportsAllDrives=true` (self identity, `drive` scope) | the service account can reach this Shared Drive |
| Gmail delegation | `GET /gmail/v1/users/{mailbox}/profile` (delegated token, impersonating `{mailbox}`) | domain-wide delegation is authorized for Gmail |
| Calendar delegation | `GET /calendar/v3/users/{mailbox}/calendarList` (delegated token, impersonating `{mailbox}`) | domain-wide delegation is authorized for Calendar |
| Docs | `GET /v1/documents/{documentId}` (self identity, `drive` scope) | the `drive` scope alone is sufficient for Docs, without a separate `documents` scope |

---

## When something is wrong

| Symptom | Almost always |
|---|---|
| *"Google Drive rejected the credentials for Shared Drive '…' (HTTP 403) — check the service account was added as a member"* | Step 3 was skipped or used the wrong email address. Re-check the Shared Drive's **Manage members** list for the service account's exact address. |
| *"Gmail rejected delegated access to '…' (HTTP 403) — check domain-wide delegation is authorized for this service account and scope"* | Step 4 was skipped, used the wrong Client ID, or the scopes do not match exactly. |
| `default_mailbox '…' is not one of the configured delegated_mailboxes` | A typo in the **Default mailbox** field — it must match one of the addresses in **Mailboxes to act as** exactly (case-insensitive). |
| Setup rejects both fields empty | Fill in at least one of Shared Drive IDs or Mailboxes — a setup that proves nothing is not allowed to succeed. |
| Setup succeeds but agents have no Google Workspace tools at all | No **Department** was chosen, or the agent is in a different one. |
| It worked for a while and then stopped | The service account key was deleted or disabled in Google Cloud Console. Create a new key (step 2) and resubmit the form. |
| A file in the Shared Drive never appears in the knowledge base | It is an unsupported type, corrupt, or over the connector's size limit. Such files are skipped and logged; the rest of the sync continues. |
| *"no mailbox given and no default_mailbox configured for this connection"* | Set a **Default mailbox**, or have the agent name one per call. |
