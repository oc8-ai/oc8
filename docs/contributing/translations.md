# Core UI Translations

How to add or update a language for oc8's own UI — the shell, buttons,
toasts, everything that isn't a capa's own content. If you're translating
a *capa's* fields and labels instead, see that capa's own `i18n/` folder
and [Developer → Packaging](../developer/packaging-and-distribution.md);
this page is specifically about `<repo root>/i18n/`.

## Recommended: translate on Weblate

For translators, the easiest path is oc8's Weblate instance at
https://translate.oc8.ai. Sign up (registration is open), open the oc8
project and its Core UI component, pick or add a language and translate in
the browser. Weblate
sends your work back as a pull request that a maintainer reviews and merges.
You don't need a development environment.

The step-by-step partner flow, the review workflow and AI first drafts are in
[Translating on translate.oc8.ai](https://docs.oc8.ai/contributing/weblate).
Strings marked "Needs editing"
are not shipped until a reviewer confirms them. The rest of this
page describes the underlying files and the pull request path, which stays
available for developers.

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

Developers can also add a language by hand (translators should prefer
Weblate, see above).

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
