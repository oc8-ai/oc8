#!/usr/bin/env node
// Exports/updates `<repo root>/i18n/<locale>.po`: one gettext catalog per
// core UI language, source strings scanned straight out of `t("English",
// "German")` call sites (the msgid is the literal English argument -- no
// invented keys, matching the capa i18n convention this mirrors).
//
// Usage:
//   node scripts/i18n-export-template.mjs <locale> [options]
//   node scripts/i18n-export-template.mjs --template [options]
//
// `--template` writes `i18n/oc8.pot` instead: every msgid, all msgstrs
// empty, a POT header. It is the "template for new translations" hosted
// Weblate reads (docs/contributing/weblate.md); CI checks it is current.
//
// Options, for an edition exporting its own strings with this same script
// (docs/contributing/translations.md, "Edition catalogs"); without them the
// script reads core's frontend/src and writes core's i18n/oc8.pot:
//   --src <dir>    scan this source tree instead of frontend/src
//   --out <dir>    write the catalogs into this directory instead of i18n/
//   --name <name>  the template's file name (<name>.pot) and the catalogs'
//                  Project-Id-Version, instead of "oc8"
// Relative paths resolve against the working directory.
//
// Safe to re-run as the UI evolves: existing translations in
// `i18n/<locale>.po` are kept, new source strings appear with an empty
// `msgstr` for someone to fill in, and strings no longer used anywhere are
// dropped. See docs/contributing/translations.md for the full workflow.

import ts from "typescript";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function usage() {
  console.error(
    "usage: node scripts/i18n-export-template.mjs <locale> | --template [--src <dir>] [--out <dir>] [--name <name>]",
  );
  process.exit(1);
}

const options = { src: null, out: null, name: null };
const positional = [];
for (let i = 2; i < process.argv.length; i++) {
  const arg = process.argv[i];
  const flag = /^--(src|out|name)$/.exec(arg);
  if (flag) {
    const value = process.argv[++i];
    if (!value) usage();
    options[flag[1]] = value;
  } else {
    positional.push(arg);
  }
}
if (positional.length !== 1) usage();
if (options.name !== null && !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(options.name)) usage();

const FRONTEND_SRC = options.src ? path.resolve(options.src) : path.join(__dirname, "..", "src");
const I18N_ROOT = options.out
  ? path.resolve(options.out)
  : path.join(__dirname, "..", "..", "i18n");
const PROJECT = options.name ?? "oc8";

const TEMPLATE_MODE = positional[0] === "--template";
const locale = positional[0];
if (!TEMPLATE_MODE && (!locale || !/^[a-z]{2}([-_][A-Z]{2})?$/.test(locale))) {
  console.error("usage: node scripts/i18n-export-template.mjs <locale> | --template");
  console.error(
    '  <locale> is a short code like "fr" or "pt_BR" (Weblate\'s style; "pt-BR" also works), used as the filename.',
  );
  process.exit(1);
}

function collectSourceStrings(root) {
  const files = [];
  function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(tsx?|jsx?)$/.test(entry.name) && !entry.name.includes(".test.")) {
        files.push(full);
      }
    }
  }
  walk(root);

  const strings = new Set();
  for (const file of files) {
    const src = fs.readFileSync(file, "utf8");
    const sf = ts.createSourceFile(
      file,
      src,
      ts.ScriptTarget.Latest,
      true,
      file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );

    function visit(node) {
      if (
        ts.isCallExpression(node) &&
        ts.isIdentifier(node.expression) &&
        node.expression.text === "t" &&
        node.arguments.length >= 1
      ) {
        const [en] = node.arguments;
        if (ts.isStringLiteral(en) || ts.isNoSubstitutionTemplateLiteral(en)) {
          if (en.text !== "") strings.add(en.text);
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(sf);
  }
  return strings;
}

// A minimal reader for the `.po` files THIS script itself writes -- not a
// general-purpose gettext parser. Multi-line/comment/obsolete-entry syntax
// is never produced by the writer below, so it never needs to be read back.
function readExistingPo(filePath) {
  if (!fs.existsSync(filePath)) return { metadata: {}, entries: new Map(), fuzzy: new Set() };
  const text = fs.readFileSync(filePath, "utf8");
  const metadata = {};
  const entries = new Map();
  const fuzzy = new Set();
  const blocks = text.split(/\n\n+/);
  for (const block of blocks) {
    const msgidMatch = block.match(/^msgid "((?:[^"\\]|\\.)*)"/m);
    const msgstrMatch = block.match(/^msgstr "((?:[^"\\]|\\.)*)"/m);
    if (!msgidMatch || !msgstrMatch) continue;
    const msgid = unescapePo(msgidMatch[1]);
    if (msgid === "") {
      // Header block: metadata lives in continuation-line strings.
      for (const line of block.split("\n")) {
        const m = line.match(/^"([^:]+): (.*)\\n"$/);
        if (m) metadata[m[1]] = m[2];
      }
      continue;
    }
    entries.set(msgid, unescapePo(msgstrMatch[1]));
    // Weblate's "Needs editing" is the gettext `#, fuzzy` flag. Keep it, or a
    // draft would silently become a shipped translation on the next export.
    if (/^#,.*\bfuzzy\b/m.test(block)) fuzzy.add(msgid);
  }
  return { metadata, entries, fuzzy };
}

function escapePo(s) {
  return s.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n").replace(/\t/g, "\\t");
}

function unescapePo(s) {
  return s.replace(/\\n/g, "\n").replace(/\\t/g, "\t").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
}

function writePo(filePath, metadata, entries, fuzzy = new Set()) {
  const header =
    `msgid ""\nmsgstr ""\n` +
    Object.entries(metadata)
      .map(([k, v]) => `"${k}: ${escapePo(v)}\\n"\n`)
      .join("") +
    "\n";
  const sorted = [...entries.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const body = sorted
    .map(
      ([msgid, msgstr]) =>
        `${fuzzy.has(msgid) ? "#, fuzzy\n" : ""}msgid "${escapePo(msgid)}"\nmsgstr "${escapePo(msgstr)}"\n`,
    )
    .join("\n");
  fs.writeFileSync(filePath, header + body);
}

const sourceStrings = collectSourceStrings(FRONTEND_SRC);

if (TEMPLATE_MODE) {
  // Deterministic on purpose (no timestamps): CI regenerates this file and
  // fails on any diff.
  const potPath = path.join(I18N_ROOT, `${PROJECT}.pot`);
  const header =
    `msgid ""\nmsgstr ""\n` +
    [
      `Project-Id-Version: ${PROJECT}`,
      "MIME-Version: 1.0",
      "Content-Type: text/plain; charset=UTF-8",
      "Content-Transfer-Encoding: 8bit",
    ]
      .map((l) => `"${l}\\n"\n`)
      .join("") +
    "\n";
  const sorted = [...sourceStrings].sort((a, b) => a.localeCompare(b));
  const body = sorted.map((msgid) => `msgid "${escapePo(msgid)}"\nmsgstr ""\n`).join("\n");
  fs.mkdirSync(I18N_ROOT, { recursive: true });
  fs.writeFileSync(potPath, header + body);
  console.log(`wrote ${path.relative(process.cwd(), potPath)} (${sorted.length} strings)`);
  process.exit(0);
}

const filePath = path.join(I18N_ROOT, `${locale}.po`);
const {
  metadata: existingMetadata,
  entries: existingEntries,
  fuzzy: existingFuzzy,
} = readExistingPo(filePath);

const metadata = {
  "Project-Id-Version": PROJECT,
  "MIME-Version": "1.0",
  "Content-Type": "text/plain; charset=UTF-8",
  "Content-Transfer-Encoding": "8bit",
  "X-Native-Name": existingMetadata["X-Native-Name"] || locale,
  "X-Flag": existingMetadata["X-Flag"] || "",
};

let added = 0;
let kept = 0;
let removed = 0;
const nextEntries = new Map();
const nextFuzzy = new Set();
for (const msgid of sourceStrings) {
  if (existingEntries.has(msgid)) {
    nextEntries.set(msgid, existingEntries.get(msgid));
    if (existingFuzzy.has(msgid)) nextFuzzy.add(msgid);
    kept++;
  } else {
    nextEntries.set(msgid, "");
    added++;
  }
}
removed = existingEntries.size - kept;

fs.mkdirSync(I18N_ROOT, { recursive: true });
writePo(filePath, metadata, nextEntries, nextFuzzy);

console.log(`wrote ${path.relative(process.cwd(), filePath)}`);
console.log(
  `  ${kept} translated, ${added} new (empty msgstr), ${removed} no longer used (dropped)`,
);
if (metadata["X-Native-Name"] === locale || !metadata["X-Flag"]) {
  console.log(
    `  fill in X-Native-Name/X-Flag at the top of the file, then translate every empty msgstr`,
  );
}
