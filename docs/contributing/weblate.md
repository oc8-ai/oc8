# Setting up Weblate for core UI translations

A one-time guide for the maintainer who connects oc8's Weblate instance to
this repository. Translators don't need it; they read
[Core UI Translations](translations.md).

The repository stays the single source of truth. Weblate reads the gettext
files from GitHub and sends translations back as pull requests against
`dev`. There is no second repository.

oc8 runs its own Weblate instance. Below, `<weblate-url>` stands for its
address (planned: `https://translate.oc8.ai`). Deploying the instance itself
is out of scope here; it lives outside this repository.

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
```

The bot needs no write access to `oc8-ai/oc8`.

## 2. Create the project and the "Core UI" component

Create a project (for example "oc8"), then one component with these
settings:

| Setting | Value |
| --- | --- |
| Component name | `Core UI` |
| Version control system | Git, or GitHub (see step 3) |
| Source code repository | `https://github.com/oc8-ai/oc8.git` |
| Repository branch | `dev` |
| File format | gettext PO file |
| File mask | `i18n/*.po` |
| Monolingual base language file | empty (the `.po` files are bilingual) |
| Template for new translations | `i18n/oc8.pot` |
| Adding new translation | Create new language file |
| Language code style | POSIX style using underscore as a separator (`pt_BR`) |

### Locale naming

The file name is the locale code. The backend (`oc8/i18n/catalog.py`) uses
the file stem as-is, and the frontend treats it as an opaque code, so both
`pt_BR` and `pt-BR` work. Weblate with the POSIX style creates `pt_BR.po`,
and `npm run i18n:export -- <locale>` accepts `pt_BR` as well as `pt-BR`. Use
one spelling per language. Mixing them creates two files.

## 3. Push back as pull requests

On the component, set:

- Push method: **GitHub pull request**
- Push branch: empty (Weblate pushes to its own fork)
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

## How a partner contributes

1. Sign up on `<weblate-url>`.
2. Open the oc8 project and the "Core UI" component.
3. Pick a language, or add one that isn't listed.
4. Translate. Strings left empty fall back to English.
5. A maintainer reviews and merges the resulting pull request into `dev`.
6. A backend restart picks up the new or changed `.po` file; the frontend
   needs no rebuild.
