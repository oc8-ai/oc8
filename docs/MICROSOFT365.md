# Connecting Microsoft 365

This is the setup guide the capa's own form points at. It walks through the
Microsoft side once — creating an app registration, granting it permissions,
and consenting to them — and then through the four things oc8's form asks for.

You need to be a **Global Administrator** (or an Application Administrator plus
a Privileged Role Administrator) in your Microsoft 365 tenant. If you are not,
the "Grant admin consent" button in step 4 will be greyed out and nothing else
in this guide will help — get whoever holds that role to do steps 1–4 with you.

Budget about 15 minutes.

---

## What kind of connection this is

oc8 connects to Microsoft 365 as an **application**, not as a person. There is
no "sign in with Microsoft" window, nobody's password is involved, and the
connection does not stop working when an employee leaves.

Two consequences worth knowing up front, because they explain several of the
choices below:

- **The app has no inbox of its own.** Because no person is signed in, every
  mail, calendar or contacts action has to say *which mailbox* it means. That is
  what the "Default mailbox" field and the `userId` tool argument are for — see
  [Which mailbox does it act on?](#which-mailbox-does-it-act-on) below.
- **Permissions are granted once, centrally, by you.** The app can reach exactly
  what you consent to in step 4 and nothing else. There is no per-user prompt
  and no way for an agent to widen its own reach later.

---

## 1. Create the app registration

1. Go to [portal.azure.com](https://portal.azure.com) and open **Microsoft Entra ID** (this is the
   service that used to be called Azure Active Directory — both names still
   appear in the portal).
2. In the left menu choose **App registrations**, then **New registration**.
3. Give it a name you will recognise later — `oc8` is fine.
4. Under **Supported account types** choose
   **Accounts in this organizational directory only (single tenant)**.
5. Leave **Redirect URI** empty. oc8 does not use one for this kind of
   connection.
6. Click **Register**.

You now land on the app's **Overview** page. Two of the four values the oc8 form
wants are on it:

| oc8 form field | Where to find it |
|---|---|
| **Azure AD tenant ID** | Overview → *Directory (tenant) ID* |
| **Application (client) ID** | Overview → *Application (client) ID* |

Both are GUIDs (`8f3c…-…-…-…-…`). Copy them somewhere for a moment.

---

## 2. Create a client secret

1. Still inside the app registration, choose **Certificates & secrets** in the
   left menu.
2. **Client secrets** tab → **New client secret**.
3. Description: anything. Expiry: pick a period you are willing to diarise —
   24 months is the usual maximum. **Write the expiry date in your calendar
   now.** When the secret expires the connection stops working, and the only
   symptom is that agents suddenly have no Microsoft 365 tools.
4. Click **Add**.

The **Value** column now shows the secret **once**. Copy it immediately — the
portal will never show it again, and it is not recoverable. (The *Secret ID* is
not the secret; you want the *Value*.)

> Rotating an expired or leaked secret later is safe: create a new one, paste it
> into the same oc8 setup form, submit. oc8 updates the existing connection in
> place rather than creating a second one.

---

## 3. Add the Graph permissions

1. Left menu → **API permissions** → **Add a permission**.
2. Choose **Microsoft Graph**.
3. Choose **Application permissions** — *not* Delegated permissions. This is the
   single most common mistake in this whole guide. Delegated permissions
   describe what an app may do *on behalf of a signed-in person*, and no person
   is signed in here, so an app registration with only delegated permissions
   mints a perfectly valid token that is then refused on every single call.
4. Tick the permissions you want from the table below and click **Add
   permissions**.

| What you want the agent to do | Permission to grant |
|---|---|
| *(always required — see the note below)* | `Organization.Read.All` |
| Read, search, send, reply, draft, move, delete mail | `Mail.ReadWrite`, `Mail.Send` |
| Read, create, update, cancel calendar events; respond to invites; free/busy | `Calendars.ReadWrite` |
| Read SharePoint / OneDrive files (also used by the knowledge base) | `Sites.Read.All`, `Files.Read.All` |
| Also write, upload or replace those files | `Sites.ReadWrite.All`, `Files.ReadWrite.All` |
| List a team's channels | `Team.ReadBasic.All` |
| List a user's Teams chats | `Chat.Read.All` |
| Search and read contacts | `Contacts.Read` |

**Grant only the rows you actually need.** A permission you never granted is a
whole category of mistake an agent cannot make. If you skip a row, the matching
tools fail with a clear "Microsoft Graph rejected the credentials (HTTP 403) —
check the app registration's Graph permissions and admin consent" message rather
than doing something surprising.

**Teams support here is read-only, and that is Microsoft's limit, not ours.** An
agent can list a team's channels and list a person's chats, but it cannot post a
Teams message. Microsoft Graph only allows sending a Teams channel or chat
message on behalf of a *signed-in person*, and — as step 3 above explains — no
person is signed in for this kind of connection. There is no Application
permission you can tick to enable it, so oc8 does not offer a Teams send tool
that would fail every time. Use mail when the agent needs to reach someone.

**Why `Organization.Read.All` is not optional.** When you submit the oc8 form it
immediately asks Microsoft one harmless read-only question — "which organisation
is this?" — to check that your permissions were really consented and not just
listed. That question is `GET /organization`, and under application permissions
it needs `Organization.Read.All`. Without it, setup fails at the last step with
a 403 even though everything else is correct. It reads your tenant's display
name and verified domains; it grants no access to mail, files or people.

---

## 4. Grant admin consent — this is a separate click

Adding a permission in step 3 does **not** activate it. The API permissions list
now shows your permissions with a status of *"Not granted for &lt;your org&gt;"*, and
in that state Microsoft will still happily issue oc8 a token — a token that is
refused on every actual call.

On the **API permissions** page, click **Grant admin consent for &lt;your
organisation&gt;** and confirm.

Every row in the list must then read **"Granted for &lt;your organisation&gt;"** with
a green check. If any row still says *Not granted*, consent did not complete —
usually because your account lacks the role at the top of this page.

This is the single most common reason a Microsoft 365 connection appears
correct and does not work.

---

## 5. Find your SharePoint site IDs (only if you want the knowledge base)

Skip this section entirely if you only want the Mail / Calendar / Teams / Office
tools. The **SharePoint site IDs** field may be left empty, and you can fill it
in later without redoing anything above.

A SharePoint site ID is not the site's URL. It is a composite of three parts
separated by commas:

```
contoso.sharepoint.com,7d1b9e5a-…-…-…-…,4f2c8a13-…-…-…-…
```

To get it for a site whose URL you know:

1. Sign in to [Microsoft Graph Explorer](https://developer.microsoft.com/graph/graph-explorer) with an
   account that can see the site.
2. Run this query, substituting your own tenant and site name:

   ```
   GET https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/Marketing
   ```

3. Copy the `"id"` value from the response. That whole comma-separated string is
   what goes in the form.

Several sites go in the same field separated by commas. Because each id already
contains commas, paste them one after another exactly as returned — oc8 splits
on commas and reassembles them.

Emptying this field later and submitting again **stops** the knowledge sync
without deleting anything already indexed.

---

## 6. Fill in the oc8 form

Capas → **Microsoft 365** → *Connect Microsoft 365*.

| Field | What to paste |
|---|---|
| **Azure AD tenant ID** | *Directory (tenant) ID* from step 1 |
| **Application (client) ID** | *Application (client) ID* from step 1 |
| **Client secret** | the **Value** from step 2 |
| **Department** | which department's agents get these tools |
| **Default mailbox** | see below — optional, but usually wanted |
| **SharePoint site IDs** | from step 5, or leave empty |

**Department is not decoration.** Agents are given tools by department. A
connection with no department set is reachable by nobody — setup succeeds, the
credentials test green, and every agent run has zero Microsoft 365 tools with no
error anywhere. Setting up the same capa for a second department creates a
second connection; it does not move the first one.

Submitting the form checks the credentials against Microsoft before saving
anything. If it comes back with an error, nothing was stored — fix the cause and
submit again.

---

## Which mailbox does it act on?

Because the app has no inbox of its own, every Mail, Calendar and Contacts tool
needs to know which mailbox it is working in. There are two ways it can find
out, and they work together:

- **Default mailbox** (this form, optional). A user principal name such as
  `info@contoso.com`, or that user's object ID. Every mail, calendar and
  contacts action uses this mailbox unless the agent explicitly names another.
  This is what most tenants want: set it once, never think about it again.
- **`userId`** (per tool call, optional). Any tool call may name a different
  mailbox, which is what lets one agent work across several mailboxes the app
  registration is permitted to reach.

If you set no default mailbox **and** an agent makes a call without naming one,
that call fails with *"no userId given and no default_user configured for this
connection"* rather than doing something to an unexpected mailbox. Leaving the
default empty is therefore a legitimate, deliberately strict choice — not an
oversight — for a setup where every call should name its target.

The Teams channel listing tool is unaffected: a channel is addressed by team and
channel ID and has no mailbox.

---

## The "On new email" trigger

An agent fixed to this connection for its tools can be started by new mail:
in the agent's trigger editor, the **On new email** tab watches one mailbox
folder and starts a run for each new mail, with the mail handed over as data.
oc8 checks every minute (`OC8_EVENT_POLL_SECONDS`) with the same app
registration as the tools, so no extra setup is needed beyond the
`Mail.Read` (or `Mail.ReadWrite`) application permission.

- **Mailbox** defaults to the default mailbox above; **Folder** to `inbox`
  (a well-known name or a folder ID).
- **Reach.** An app-only `Mail.Read` permission covers every mailbox in the
  tenant, so a trigger can watch any of them, just as the tools can name any
  `userId`. To narrow that, an Exchange admin can scope the app registration
  to a mail-enabled group with an **Application Access Policy**
  (`New-ApplicationAccessPolicy`) or **RBAC for Applications**; Graph then
  refuses every mailbox outside the group, for the tools and the trigger
  alike.
- **A signed-in account** (instead of an app registration) always watches
  its own mailbox; the Mailbox setting is ignored for it.
- If the connection is removed or replaced, the trigger switches itself off;
  switch it on again once the agent has its new connection.

---

## Checking it really works

The form's own submission proves the credential and the consent. To satisfy
yourself the rest works, the four calls below cover every shape this capa
uses. Run them in Graph Explorer, or against a token minted from your app
registration:

| Check | Call | Proves |
|---|---|---|
| Consent | `GET /v1.0/organization` | `Organization.Read.All` is consented — the same call oc8's setup makes |
| Mail | `GET /v1.0/users/{your-default-mailbox}/messages?$top=1` | mail permissions, and that the mailbox address is right |
| Files | `GET /v1.0/sites/{site-id}/drive/root/children` | the site ID is real and readable |
| Download | `GET /v1.0/drives/{drive-id}/items/{item-id}/content` | file downloads work — note this answers `302` and redirects, which is normal |

---

## When something is wrong

| Symptom | Almost always |
|---|---|
| *"Microsoft Graph rejected the credentials (HTTP 403) — check the app registration's Graph permissions and admin consent"* | Step 4 was not done, or was done before a permission was added in step 3. Re-open API permissions and confirm every row reads *Granted*. |
| Setup fails with a 403 although every listed permission says *Granted* | `Organization.Read.All` is missing. See step 3. |
| Every call is refused although consent looks complete | Step 3 used **Delegated** permissions instead of **Application** permissions. Delete them, add the Application ones, consent again. |
| It worked for a year and then stopped | The client secret expired. Step 2, then paste the new value into the same form. |
| Setup succeeds but agents have no Microsoft 365 tools at all | No **Department** was chosen, or the agent is in a different one. |
| *"no userId given and no default_user configured for this connection"* | Set a **Default mailbox**, or have the agent name one per call. |
| A `.docx` or `.xlsx` in SharePoint never appears in the knowledge base | It is corrupt, password-protected, or over 5 MB. Such files are skipped and logged; the rest of the sync continues. |
| The agent cannot send a Teams message and no permission seems to help | It never can. Microsoft Graph has no Application permission for posting a Teams channel or chat message, so oc8 ships no such tool. See step 3. |
| *"Microsoft Graph item not found (HTTP 404)"* on the knowledge base | A SharePoint site ID is mistyped. Site IDs are only checked for reachability at setup, not one by one. |
