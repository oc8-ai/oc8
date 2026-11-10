# Core UI Translations

How to add or update a language for oc8's own UI — the shell, buttons,
toasts, everything that isn't a capa's own content. If you're translating
a *capa's* fields and labels instead, see that capa's own `i18n/` folder
and [Developer → Packaging](../developer/packaging-and-distribution.md);
this page is specifically about `<repo root>/i18n/`.

## Two ways to contribute

| | In the browser on translate.oc8.ai | With Git and a pull request |
|---|---|---|
| For | Translators and partners | Developers |
| You need | A free account | A fork, Node.js and a `.po` editor |
| Your work reaches oc8 | Weblate opens the pull request for you | You open the pull request |

Both end in the same place: a reviewed change to `i18n/<locale>.po` on
`dev`, shipped with the next release.

## Option 1: translate in the browser (translate.oc8.ai)

1. **Sign up** at [translate.oc8.ai](https://translate.oc8.ai) and confirm
   the email Weblate sends you. Registration is open.
2. **Open the project:** *Projects → oc8 → Core UI*.
3. **Pick your language.** If it is listed, click it. If not, choose
   *Start new translation*, select the language and confirm — Weblate
   creates the file for you.
4. **Translate.** Click *Translate* and work through the strings. Each one
   shows the English source, nearby strings and, where available, glossary
   terms and suggestions. Keep placeholders such as `{name}` or `{count}`
   exactly as they are, and don't translate product names (oc8, Copilot,
   Odoo, …).
5. **Review drafts.** Some languages (French, Dutch, Spanish) start with a
   first draft marked *Needs editing*. Use the filter *Strings needing
   action → Needs editing*, correct each draft and save it; saving clears
   the mark. Drafts that are still marked are **not** shipped — users see
   the English text until someone confirms the translation.
6. **Not sure?** Leave a comment on the string instead of guessing, or
   add a *suggestion* for someone else to accept.
7. **That's it.** Weblate collects the changes and opens a pull request
   against `oc8-ai/oc8`. A maintainer reviews and merges it; your language
   ships with the next oc8 release.

Want to look after a language as its reviewer, or get a draft for a new
language? Ask in the pull request or contact the maintainers. The
maintainer-side setup (bot account, review workflow, AI drafts) is in
[Weblate setup](weblate.md#contributing-as-a-translation-partner).

## Option 2: translate with Git and a pull request

For developers who prefer working in the repository. The steps are under
[Adding a new language](#adding-a-new-language) and
[Updating an existing language](#updating-an-existing-language) below:
export the catalog, translate it in any `.po` editor (for example
Poedit), restart the backend to check it, and open a pull request against
`dev`. If a language is already maintained on translate.oc8.ai, use the
browser instead so the two don't overwrite each other.

## How it works

One gettext `.po` catalog per language, at `<repo root>/i18n/<locale>.po`
(e.g. `i18n/de.po`, `i18n/fr.po`). `msgid` is the literal English string as
it appears in the source — content-as-key, no invented translation keys,
the same convention a capa's own `i18n/*.po` catalogs use
([Core Architecture](architecture.md)). There is no `i18n/en.po`: English
*is* the source text, so it needs no lookup.

At runtime, `backend/src/oc8/i18n/catalog.py` reads every `.po` file under
`i18n/` and `GET /api/v1/i18n/core` serves them as one JSON payload the
frontend fetches once at startup. The `i18n/` folder is mounted read-only
into the `backend` container (`docker-compose.yml`) — **dropping in a new
or edited `.po` file only needs a `backend` restart, never a frontend
rebuild.** A locale missing a given `msgid` (or missing the whole file)
falls back to the English source string, exactly like a capa's catalogs
do.

German is a partial exception, for now: every `t("English", "German")`
call site in the frontend still carries its German text as a literal
second argument, so German keeps working even before `i18n/de.po` is read
at all. A catalog entry for German, when present, overrides that literal —
so `i18n/de.po` is genuinely editable-without-a-code-change too, it just
isn't the only thing keeping German alive. Every other language has no
such literal and is *entirely* sourced from its `.po` file.

## Adding a new language

Option 2, step by step (translators: see [Option 1](#option-1-translate-in-the-browser-translateoc8ai)).

1. From `frontend/`, run:

   ```sh
   npm run i18n:export -- <locale>
   ```

   `<locale>` is a short code like `fr`, `es`, or `pt_BR` — it becomes the
   filename. Use the underscore form (`pt_BR`) so the file matches what
   Weblate creates. This scans every `t("English", …)` call in the frontend and
   writes `i18n/<locale>.po` with one empty `msgstr` per string.

2. Open `i18n/<locale>.po`. Fill in the header:

   ```po
   "X-Native-Name: Français\n"
   "X-Flag: 🇫🇷\n"
   ```

   `X-Native-Name` is what the language switcher shows; `X-Flag` is the
   emoji next to it.

3. Translate. Leave a `msgstr` empty for anything you don't want to
   translate yet — it just falls back to the English `msgid`, it never
   errors.

4. Restart the backend (`docker compose up -d backend` after a fresh
   `docker compose build backend` if the image itself needs rebuilding,
   or just restarting it against an already-mounted checkout in dev) and
   confirm the new language appears in the language switcher and reads
   correctly.

5. Open a pull request against `i18n/<locale>.po` (see
   [Opening a pull request](index.md#opening-a-pull-request)). No other
   file needs to change — that's the whole point of the catalog being
   data, not code.

## The template

`i18n/oc8.pot` lists every English string with empty translations. It is what
Weblate offers for new languages. Regenerate it after you add or change a
`t("English", …)` string:

```sh
npm run i18n:template
```

CI fails a change to `frontend/src` if the committed template is out of date.

## Updating an existing language

Run the same command again:

```sh
npm run i18n:export -- de
```

It merges rather than overwrites: translations already in the file are
kept, new English strings the UI has picked up since you last ran it
appear with an empty `msgstr`, and strings no longer used anywhere are
dropped. Fill in the new blanks and open a PR.

## A rough edge worth knowing about

Content-as-key means the exact same English string always resolves to
the exact same translation, wherever it appears — gettext and Odoo work
the same way. Two UI call sites that happen to share an English string
("Runs", say) but want different translations in your language can't
both be satisfied by one `msgid`. When the export script finds the core
team's own German catalog disagreeing with itself this way for a given
string, it leaves that `msgstr` empty rather than guessing, and that
call site quietly keeps using its own inline literal instead. If you hit
the same ambiguity in your language, either translation choice is fine —
there's no clean fix short of giving oc8 a `msgctxt` scheme, which hasn't
been needed yet.
