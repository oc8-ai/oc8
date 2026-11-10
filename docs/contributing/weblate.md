# Setting up Weblate for core UI translations

A one-time guide for the maintainer who connects oc8's Weblate instance to
this repository. Translators don't need it; they read
[Core UI Translations](translations.md).

The repository stays the single source of truth. Weblate reads the gettext
files from GitHub and sends translations back as pull requests against
`dev`. There is no second repository.

oc8 runs its own Weblate instance at **https://translate.oc8.ai** (project
slug `oc8`, component `core-ui`). Deploying the instance itself is out of
scope here; the operator guide lives in
[github.com/oc8-ai/translate](https://github.com/oc8-ai/translate).

## What the repository provides

- `i18n/oc8.pot`: the template, every English `msgid` with an empty
  `msgstr`. Regenerate it with `npm run i18n:template` from `frontend/`. CI
  (`.github/workflows/i18n-template.yml`) fails a push or pull request that
  changes `frontend/src` without updating it.
- `i18n/<locale>.po`: one bilingual catalog per language (the English
  `msgid` sits next to the translation), e.g. `i18n/de.po`.

## 1. GitHub bot account

The "GitHub pull request" push method forks the repository under a bot
account and opens the pull request from that fork. The instance therefore
needs a GitHub account with a personal access token that can fork `oc8-ai/oc8`
and open pull requests. Configure it in the instance's environment:

```sh
WEBLATE_GITHUB_USERNAME=<bot account>
WEBLATE_GITHUB_TOKEN=<token>
WEBLATE_GITHUB_HOST=api.github.com
```

`WEBLATE_GITHUB_HOST=api.github.com` is required alongside the username and
token; without it Weblate doesn't use the GitHub API for pull requests.

The token is a **classic** personal access token with the scopes
**`public_repo`** and **`workflow`**. `workflow` is needed because the bot's
fork receives upstream commits that touch `.github/workflows`, and GitHub
rejects a push containing such changes unless the token carries that scope.
The bot needs no write access to `oc8-ai/oc8`.

## 2. Create the project and the "Core UI" component

Create a project (for example "oc8"), then one component with these
settings:

| Setting | Value |
| --- | --- |
| Component name | `Core UI` |
| Version control system | GitHub pull request |
| Source code repository | `https://github.com/oc8-ai/oc8.git` |
| Repository branch | `dev` |
| File format | gettext PO file |
| File mask | `i18n/*.po` |
| Monolingual base language file | empty (the `.po` files are bilingual) |
| Template for new translations | `i18n/oc8.pot` |
| Adding new translation | Create new language file |
| Language code style | POSIX style using underscore as a separator (`pt_BR`) |
| Translation file line wrapping | No wrapping (otherwise Weblate rewraps `de.po`) |
| Location comments | Off (no `#:` source references) |
| Remove obsolete entries | On |
| Component license | `LGPL-3.0-or-later` |

### Locale naming

The file name is the locale code. The backend (`oc8/i18n/catalog.py`) uses
the file stem as-is, and the frontend treats it as an opaque code, so both
`pt_BR` and `pt-BR` work. Weblate with the POSIX style creates `pt_BR.po`,
and `npm run i18n:export -- <locale>` accepts `pt_BR` as well as `pt-BR`. Use
one spelling per language. Mixing them creates two files.

## 3. Push back as pull requests

On the component, set:

- Version control system: **GitHub pull request**
- Push branch: `translations`. It must differ from `dev`, the repository
  branch, or Weblate has nothing to open a pull request from.
- Target branch: `dev` (the component's repository branch)
- Commit message: `chore(i18n): update {{ language_name }} translations from Weblate`

`{{ language_name }}` is the Weblate template variable for the language.
`{{ component_name }}` and `{{ project_name }}` also exist. Check the
variable list in the "Commit, add, delete, merge and add-on messages" help
text of your Weblate version, and read one resulting commit to confirm the
prefix matches the repository's conventional commits
([Git guidelines](git-guidelines.md)).

A maintainer reviews and merges each Weblate pull request into `dev`.

## 4. Webhook

So new strings show up as soon as `dev` changes, add a webhook in the GitHub
repository settings:

- Payload URL: `<weblate-url>/hooks/github/`
- Content type: `application/json`
- Events: push only

## 5. Add-ons

On the component, enable:

- **Update PO files to match POT (msgmerge)**: new strings from
  `i18n/oc8.pot` reach every language file.
- Optional: **Squash Git commits**, to keep each pull request at one commit
  per language.

## 6. New languages and the `X-Native-Name` / `X-Flag` headers

The language switcher shows `X-Native-Name` and `X-Flag` from each `.po`
header. They are optional: the backend falls back to the locale code for the
name and to an empty flag when they are missing. So a language created in
Weblate works immediately and shows up as, say, `pt_BR` without a flag. No
code change is needed.

Weblate does not give translators a way to edit these custom headers. After
merging a new language's pull request, add them to the file in a follow-up
commit:

```po
"X-Native-Name: Português (Brasil)\n"
"X-Flag: 🇧🇷\n"
```

Weblate keeps custom header lines when it commits later changes.

> **Warning:** don't run `npm run i18n:export -- <locale>` on a language
> that Weblate manages. The script rewrites the file with its own minimal
> header and drops comments, which Weblate then has to restore. Let Weblate
> and the msgmerge add-on keep those files current. `i18n:export` stays for
> developers working without Weblate.

## 7. Capas

A capa's own `i18n/` catalogs are separate from the core UI and are not
part of this component. Each can become an additional Weblate component
later (same settings, different repository path and mask). Nothing is set up
for them yet.

## Contributing as a translation partner

This is the flow partners follow. Share it as-is.

1. Sign up at https://translate.oc8.ai (registration is open) and confirm
   the email address.
2. Open the project **oc8**, then the component **Core UI**.
3. Pick your language, or choose **Start new translation** to add one that
   isn't listed.
4. Translate or review strings. The **Needs editing** filter lists strings
   that still want a human check, such as AI drafts (see below).
5. Save. Strings marked **Needs editing** are *not* kept out of the
   shipped catalogue: Weblate stores them in the `.po` file with the gettext
   `fuzzy` flag, and `backend/src/oc8/i18n/catalog.py` does not look at that
   flag. It loads every entry with a non-empty `msgstr`, fuzzy or not, so
   once the pull request is merged users of that language see the draft.
   Only empty strings fall back to English. Treat "Needs editing" as a
   review marker, not a gate, and don't merge drafts you aren't willing to
   ship.
6. A maintainer merges the Weblate pull request into `dev`, and the language
   ships with the next release. A backend restart picks up a new or changed
   `.po` file; the frontend needs no rebuild.

### Optional review workflow

By default any registered user can translate directly. For a stricter
process, enable **Enable reviews** in the project's settings (Workflow).
Strings then move from "Translated" to "Approved" once a reviewer accepts
them. Approval is tracked in Weblate; the catalogue the backend serves
doesn't distinguish approved from unapproved strings.

Review rights come from the **Reviewers** access-control group; Weblate also
creates per-language team groups. Permissions differ by role:

- Users without review rights can still leave **suggestions**. A suggestion
  doesn't change the `.po` file until someone with translate rights accepts
  it. Users with translate rights save **direct translations**, which are
  committed.
- A partner becomes reviewer for their language when a maintainer adds them
  to the reviewer group for that language (or to the project-wide
  **Reviewers** group) in the Weblate administration. Ask in the pull
  request or via a maintainer.

## AI first drafts (optional)

A new language doesn't have to start from an empty file. Weblate has built-in
machine translation services, in current versions among them OpenAI, Azure
OpenAI, DeepL, Google, LibreTranslate and **Anthropic** (documented at
[Weblate's machine translation page](https://docs.weblate.org/en/latest/admin/machine.html);
the instance runs Weblate 2026.11.1, which includes it). The instance has no
API endpoint that lists the configured services, so check
**Administration → Machine translation** in the UI.

Configure a service in one of two places, each with an API key:

- Site-wide: **Administration → Machine translation**.
- Per project: **Project → Settings → Automatic suggestions**.

To fill a language, open the translation and use **Operations → Automatic
translation**:

- Mode: **Add as translation needing edit**.
- Filter: untranslated strings.
- Source: the machine translation service you configured.

To get drafts for strings added later, enable the **Automatic translation**
add-on on the component, with the same mode, so new strings arrive as
"Needs editing" drafts.

**Rule:** AI drafts are always "Needs editing", never approved, and a human
partner validates them. Never use a mode that saves them as final
translations. Because the backend serves fuzzy entries (see step 5 above), a
draft that is merged unreviewed is shown to users, so review before the pull
request is merged.

The `nl` and `es` drafts were seeded this way, as needs-editing drafts.

## Enterprise edition strings

*Planned.* Enterprise UI strings live in the private enterprise repository
and aren't translated through the public project yet. The planned approach
is a separate private Weblate project, readable only by contracted
partners. Nothing is set up, and there is no committed date.
