#!/usr/bin/env node
// Vendors a pinned Pyodide release into public/pyodide/ for "Run Python" in
// the chat (served by the app itself: no CDN at run time, so air-gapped
// installs work and the sandbox needs no network).
//
//   node scripts/vendor-pyodide.mjs          verify; download only what is missing
//                                            or wrong (offline-safe when complete)
//   node scripts/vendor-pyodide.mjs --check  verify only; exit 1 on any mismatch
//
// OC8_SKIP_PYODIDE=1 skips it all: an image without Python ("Run" then says
// Python is not installed on this server).
//
// The files are not committed (about 28 MB). `npm run build`/`dev` run this
// first (prebuild/predev), and so does frontend/Dockerfile.
//
// Every file's SHA-256 is pinned below; a download that does not match is
// thrown away and the script fails. The wheel hashes are also compared with
// the (pinned) lock file, so the list cannot drift from the release. The set
// is the core runtime plus numpy, pandas and matplotlib with their
// dependencies as the lock file names them.
//
// Air-gapped installs: run this on a connected machine and copy
// public/pyodide/ (see docs/DEPLOY.md).

import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const PYODIDE_VERSION = "0.29.5";
const BASE = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const TARGET = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "pyodide");

/** The runtime: loader, interpreter, standard library, package index. */
const CORE = {
  "pyodide.js": "7f832a350240263d9946a9c3c877f7bcdab6c37d6dc65f72cd3be5905dca62dd",
  "pyodide.asm.js": "356c42f69e1695397e9d8670bd3c2e678248cde76e18bdae1731e848537b47d7",
  "pyodide.asm.wasm": "54309a5a2cfd757b1f0fdbc9093c92503f7b341500110329d932487183912718",
  "python_stdlib.zip": "831fd1e535084b972f87c6a35275de3d236efd5166f5e77ce89497710635e73c",
  "pyodide-lock.json": "14d2c2dba101277999e17135e653d8f15389ad1437f53eae213bf0c3cdff723d",
};

/** The packages offered on first import, with everything they depend on. */
const ROOTS = ["numpy", "pandas", "matplotlib"];
const WHEELS = {
  "contourpy-1.3.1-cp313-cp313-pyemscripten_2025_0_wasm32.whl":
    "65f63bc91d211c5d44e2e81dab7397b2da5f195688cf2785586be3cba6d732a7",
  "cycler-0.12.1-py3-none-any.whl":
    "6eedeaff76b9be1d5e5b759fa9b84c3b384b3684cabea98defd97e974112865f",
  "fonttools-4.56.0-py3-none-any.whl":
    "30d4590e0bac3ef0445a4e0fa25600e8b4fff54bb028b02666013c26fde42965",
  "kiwisolver-1.4.8-cp313-cp313-pyemscripten_2025_0_wasm32.whl":
    "23e3ec0666ab7e747f902c18f9009103b094c8424458aaeeb2504338dac9100f",
  "matplotlib-3.8.4-cp313-cp313-pyemscripten_2025_0_wasm32.whl":
    "b919d426f1703c055b3c12a06871af40b8eb07d82339f4f9d7e45279fd93e81f",
  "numpy-2.2.5-cp313-cp313-pyemscripten_2025_0_wasm32.whl":
    "800c98edc0c864dfa49f07005680c699b4b42b84eae1f8cb19d35b3634e7f05c",
  "packaging-26.2-py3-none-any.whl":
    "7a0e80a4de677fefa2782ea4d34063708a3ac3d54469298f22cedcc4c7bebb4b",
  "pandas-2.3.3-cp313-cp313-pyemscripten_2025_0_wasm32.whl":
    "2579a3d8e9f040421836365ccf7886fc4054c87381d4d481840a97785cd78923",
  "pillow-11.3.0-cp313-cp313-pyemscripten_2025_0_wasm32.whl":
    "57d88e2ac283c21830b4ee920cb73fbbf5c46df62d967089fce5fec46548bd7b",
  "pyparsing-3.2.1-py3-none-any.whl":
    "67a97ce758343bf6e651a1cc63731f19fd6720a2d6617eab71528021552ee3dc",
  "python_dateutil-2.9.0.post0-py2.py3-none-any.whl":
    "1fe3a0346ef21bc85b295227256d6aed72aa8116178b019fc5ae5d57fbd213cf",
  "pytz-2025.2-py2.py3-none-any.whl":
    "d7fb2f11cf7f8dd17e652d5494189f520ee086e9fc71aef4cbb978f5dcfc2878",
  "six-1.17.0-py2.py3-none-any.whl":
    "bc830cfdc71a224b6d0e4337183a87ea53f21b6623deca5771ee3bc866fd8da2",
};

const FILES = { ...CORE, ...WHEELS };

const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

async function intact(name) {
  const path = join(TARGET, name);
  if (!existsSync(path)) return false;
  return sha256(await readFile(path)) === FILES[name];
}

const AIR_GAPPED =
  "Without internet access: run `node scripts/vendor-pyodide.mjs` in frontend/ on a " +
  "connected machine and copy frontend/public/pyodide/ here (see docs/DEPLOY.md, " +
  '"Running Python in the chat").';

async function download(name) {
  let res;
  try {
    res = await fetch(BASE + name);
  } catch (error) {
    throw new Error(
      `cannot download ${name} from ${BASE} (${error.cause?.code ?? error.message}). ${AIR_GAPPED}`,
    );
  }
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
  const bytes = Buffer.from(await res.arrayBuffer());
  const got = sha256(bytes);
  if (got !== FILES[name]) {
    throw new Error(`${name}: SHA-256 ${got} does not match the pinned ${FILES[name]}`);
  }
  // Written next to the target and renamed: a broken run leaves no half file.
  const partial = join(TARGET, `${name}.partial`);
  await writeFile(partial, bytes);
  await rename(partial, join(TARGET, name));
  return bytes.length;
}

/** The wheels the lock file says ROOTS need, against the pinned list. */
async function checkLock() {
  const lock = JSON.parse(await readFile(join(TARGET, "pyodide-lock.json"), "utf8"));
  const needed = new Set();
  const visit = (name) => {
    if (needed.has(name)) return;
    const entry = lock.packages[name];
    if (!entry) throw new Error(`pyodide-lock.json has no package ${name}`);
    needed.add(name);
    entry.depends.forEach(visit);
  };
  ROOTS.forEach(visit);
  const fromLock = new Map(
    [...needed].map((n) => [lock.packages[n].file_name, lock.packages[n].sha256]),
  );
  for (const [file, hash] of fromLock) {
    if (WHEELS[file] !== hash) throw new Error(`${file}: the lock file and the pinned list differ`);
  }
  for (const file of Object.keys(WHEELS)) {
    if (!fromLock.has(file)) throw new Error(`${file} is pinned but no root package needs it`);
  }
}

async function main() {
  const mode = process.argv[2];
  if (mode && mode !== "--check") {
    console.error("usage: vendor-pyodide.mjs [--check]");
    process.exit(2);
  }
  if (process.env.OC8_SKIP_PYODIDE === "1") {
    console.log("vendor-pyodide: skipped (OC8_SKIP_PYODIDE=1), the app ships without Python");
    return;
  }
  await mkdir(TARGET, { recursive: true });
  // A run that died mid-download leaves its .partial file behind.
  for (const entry of await readdir(TARGET)) {
    if (entry.endsWith(".partial")) await rm(join(TARGET, entry), { force: true });
  }
  let total = 0;
  let fetched = 0;
  for (const name of Object.keys(FILES)) {
    if (!(await intact(name))) {
      if (mode === "--check") {
        console.error(`${name}: missing or not the pinned file. ${AIR_GAPPED}`);
        process.exitCode = 1;
        continue;
      }
      await rm(join(TARGET, name), { force: true });
      fetched += await download(name);
      console.log(`fetched ${name}`);
    }
    total += (await stat(join(TARGET, name))).size;
  }
  if (process.exitCode) return;
  await checkLock();
  const mb = (n) => (n / 1024 / 1024).toFixed(1);
  console.log(
    `Pyodide ${PYODIDE_VERSION}: ${Object.keys(FILES).length} files, ${mb(total)} MB in public/pyodide` +
      (fetched ? ` (${mb(fetched)} MB downloaded)` : ""),
  );
}

main().catch((error) => {
  console.error(`vendor-pyodide: ${error.message}`);
  process.exit(1);
});
