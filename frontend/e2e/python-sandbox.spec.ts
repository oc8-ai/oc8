// "Run Python" in a real browser (spec §5.3, §9): Pyodide from the vendored
// files (public/pyodide), started by the real runner (bundled from source)
// in the real sandboxed frame, under the app's own response CSP. Python that
// tries every way out must reach no host: a second local "outside" server
// counts every TCP connection, and must see none. Runs in Chromium and
// Firefox (playwright.config.ts). Needs the files: `npm run vendor:pyodide`
// (they are not in git); without them the spec is skipped.
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import http from "node:http";
import net from "node:net";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";
import { build } from "esbuild";

import { withFrameAncestors } from "../src/server/frame-ancestors";

const PYODIDE = fileURLToPath(new URL("../public/pyodide/", import.meta.url));

let outside: net.Server;
let app: http.Server;
let hits: string[] = [];
let assets: string[] = [];
/** Requests for vendored files matching this never get an answer. */
let hang: RegExp | null = null;
let OUT = "";
let APP = "";
let bundle = "";

test.describe.configure({ mode: "serial", timeout: 180_000 });
test.skip(
  !existsSync(`${PYODIDE}pyodide.asm.wasm`),
  "Pyodide is not vendored: run `npm run vendor:pyodide` in frontend/ first.",
);

test.beforeAll(async () => {
  const built = await build({
    entryPoints: [
      fileURLToPath(new URL("../src/components/conversation/python/runner.ts", import.meta.url)),
    ],
    bundle: true,
    format: "iife",
    globalName: "Runner",
    write: false,
  });
  bundle = built.outputFiles[0].text;
  outside = net.createServer((socket) => {
    hits.push("connection");
    socket.on("data", (d) => hits.push(String(d).split("\r\n")[0]));
    socket.on("error", () => undefined);
    setTimeout(() => socket.destroy(), 50);
  });
  await new Promise<void>((r) => outside.listen(0, "127.0.0.1", r));
  OUT = `127.0.0.1:${(outside.address() as AddressInfo).port}`;
  app = http.createServer(async (req, res) => {
    const url = req.url ?? "/";
    if (url.startsWith("/pyodide/")) {
      const name = decodeURIComponent(url.slice("/pyodide/".length));
      assets.push(name);
      if (hang?.test(name)) return;
      if (!/^[A-Za-z0-9][A-Za-z0-9._+-]*$/.test(name)) {
        res.writeHead(404).end();
        return;
      }
      try {
        const bytes = await readFile(PYODIDE + name);
        res.writeHead(200, { "content-length": bytes.length }).end(bytes);
      } catch {
        res.writeHead(404).end();
      }
      return;
    }
    if (url.startsWith("/api/")) hits.push(`app api ${url}`);
    const js = url === "/runner.js";
    const body = js
      ? bundle
      : "<!doctype html><body><script src='/runner.js'></script><script>window.said=[];" +
        'document.addEventListener("securitypolicyviolation",e=>said.push("violation "+e.violatedDirective));' +
        "</script></body>";
    const headed = await withFrameAncestors(
      new Request(`http://localhost${url}`),
      new Response(body, {
        headers: { "content-type": js ? "text/javascript" : "text/html; charset=utf-8" },
      }),
      {},
    );
    res.writeHead(200, Object.fromEntries(headed.headers));
    res.end(await headed.text());
  });
  await new Promise<void>((r) => app.listen(0, "localhost", r));
  APP = `http://localhost:${(app.address() as AddressInfo).port}`;
});

test.afterAll(() => {
  app.close();
  outside.close();
});

test.beforeEach(() => {
  hits = [];
  assets = [];
  hang = null;
});

type Result = {
  stdout: string;
  stderr: string;
  result: string | null;
  images: string[];
  tables: { columns: string[]; rows: string[][] }[];
  timedOut: boolean;
};

/** Runs each of `codes` in turn in one page (one frame); returns the results,
 *  or the error message of a run that failed. */
async function run(
  page: Page,
  codes: string[],
  timeoutMs?: number,
  extra: { mounts?: Record<number, string>; loadTimeoutMs?: number } = {},
) {
  await page.goto(`${APP}/`);
  return page.evaluate(
    async ({ codes, timeoutMs, extra }) => {
      const R = (
        window as unknown as {
          Runner: typeof import("../src/components/conversation/python/runner");
        }
      ).Runner;
      const out: (Result | string)[] = [];
      for (const [i, code] of codes.entries()) {
        const text = extra.mounts?.[i];
        const files = text
          ? [{ name: "secret.csv", bytes: new TextEncoder().encode(text).buffer as ArrayBuffer }]
          : [];
        try {
          out.push(
            await R.runPython(code, {
              signal: new AbortController().signal,
              timeoutMs,
              files,
              loadTimeoutMs: extra.loadTimeoutMs,
            }),
          );
        } catch (e) {
          out.push(`error: ${(e as Error).message}`);
        }
      }
      return out;
    },
    { codes, timeoutMs, extra },
  );
}

test("runs Python offline and nothing gets out", async ({ page }) => {
  await page.goto(`${APP}/`);
  // Nothing is fetched before the first run.
  await page.waitForTimeout(300);
  expect(assets).toEqual([]);
  const probe = `
import js
def tries(name, f):
    try:
        f()
        print(name, "OPEN")
    except BaseException as e:
        print(name, "blocked")
async def atries(name, f):
    try:
        await f()
        print(name, "OPEN")
    except BaseException as e:
        print(name, "blocked")

def urllib_out():
    import urllib.request
    urllib.request.urlopen("http://${OUT}/urllib", timeout=3)
tries("urllib", urllib_out)
from pyodide.http import pyfetch
await atries("pyfetch", lambda: pyfetch("http://${OUT}/pyfetch"))
await atries("pyfetch api", lambda: pyfetch("/api/v1/auth/me"))
await atries("js.fetch", lambda: js.fetch("http://${OUT}/fetch"))
await atries("js.fetch api", lambda: js.fetch("${APP}/api/v1/auth/me"))
def xhr():
    x = js.XMLHttpRequest.new()
    x.open("GET", "http://${OUT}/xhr")
    x.send()
tries("xhr", xhr)
tries("websocket", lambda: js.WebSocket.new("ws://${OUT}/ws"))
tries("webrtc", lambda: js.RTCPeerConnection.new(js.JSON.parse('{"iceServers":[{"urls":"turn:${OUT}?transport=tcp","username":"u","credential":"c"}]}')))
tries("eventsource", lambda: js.EventSource.new("http://${OUT}/es"))
tries("worker", lambda: js.Worker.new("data:text/javascript,1"))
tries("parent", lambda: js.parent.document)
tries("top", lambda: js.top.location)
tries("postMessage", lambda: js.postMessage("forged"))
tries("send", lambda: js.eval("send"))
tries("importScripts", lambda: js.importScripts("data:text/javascript,1"))
def js_fetch_eval():
    js.eval("new XMLHttpRequest()")
tries("eval xhr", js_fetch_eval)
print("sum", sum(range(10)))
import sys
print("to stderr", file=sys.stderr)
6 * 7
`;
  const [result] = await run(page, [probe]);
  await page.waitForTimeout(1500);
  expect(typeof result, String(result)).toBe("object");
  const r = result as Result;
  for (const name of [
    "urllib",
    "pyfetch",
    "pyfetch api",
    "js.fetch",
    "js.fetch api",
    "xhr",
    "websocket",
    "webrtc",
    "eventsource",
    "worker",
    "parent",
    "top",
    "postMessage",
    "send",
    "importScripts",
    "eval xhr",
  ]) {
    expect(r.stdout, name).toContain(`${name} blocked`);
  }
  expect(r.stdout).not.toContain("OPEN");
  expect(r.stdout).toContain("sum 45");
  expect(r.stderr).toContain("to stderr");
  expect(r.result).toBe("42");
  expect(r.timedOut).toBe(false);
  expect(hits).toEqual([]);
  // Only vendored files, fetched by the page.
  for (const name of assets) expect(name).toMatch(/^(pyodide|python_stdlib)/);
});

test("pandas, numpy and matplotlib load on first import", async ({ page }) => {
  // A cold Firefox compiles pandas' native modules for several seconds, more
  // on a busy machine (the wall clock starts after that, see preload).
  test.setTimeout(300_000);
  const [first, second] = await run(page, [
    `
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
plt.plot(np.arange(5), np.arange(5) ** 2)
plt.title("squares")
pd.DataFrame({"n": [1, 2, 3], "square": [1, 4, 9]})
`,
    "raise ValueError('bad input')",
  ]);
  const r = first as Result;
  expect(r.stderr).toBe("");
  expect(r.tables).toEqual([
    {
      columns: ["n", "square"],
      rows: [
        ["1", "1"],
        ["2", "4"],
        ["3", "9"],
      ],
    },
  ]);
  expect(r.images).toHaveLength(1);
  expect(r.images[0].startsWith("data:image/png;base64,")).toBe(true);
  expect(assets).toEqual(expect.arrayContaining([expect.stringMatching(/^pandas-.*\.whl$/)]));
  const e = second as Result;
  expect(e.stderr).toContain("ValueError: bad input");
  expect(e.stderr).toContain('File "<answer>"');
  expect(hits).toEqual([]);
});

test("the wall clock stops endless code and the next run works", async ({ page }) => {
  const [stuck, after] = await run(page, ["while True:\n    pass", "print('again')"], 3000);
  expect((stuck as Result).timedOut).toBe(true);
  expect((after as Result).stdout).toBe("again\n");
});

test("chat uploads are mounted read-only", async ({ page }) => {
  await page.goto(`${APP}/`);
  const result = await page.evaluate(async () => {
    const R = (
      window as unknown as { Runner: typeof import("../src/components/conversation/python/runner") }
    ).Runner;
    const bytes = new TextEncoder().encode("a,b\n1,2\n").buffer;
    return R.runPython(
      `
print(open("sales.csv").read().strip())
try:
    open("sales.csv", "w").write("x")
    print("write OPEN")
except OSError:
    print("write blocked")
`,
      { signal: new AbortController().signal, files: [{ name: "../sales.csv", bytes }] },
    );
  });
  expect(result.stdout).toBe("a,b\n1,2\nwrite blocked\n");
});

test("nothing a run leaves behind reaches the next run", async ({ page }) => {
  const [hook, mounted, after, mark] = await run(
    page,
    [
      // A hook on the file system and a module-level mark, left behind.
      `
import js, sys, pyodide_js
sys.oc8_mark = "run 1"
original = pyodide_js.FS.writeFile
def spy(path, data, *rest):
    js.globalThis.leaked = str(path)
    return original(path, data, *rest)
pyodide_js.FS.writeFile = spy
print("hooked")
`,
      `print(open("secret.csv").read())`,
      `
import js
print("leaked", getattr(js.globalThis, "leaked", None))
import os
print("files", sorted(f for f in os.listdir(".") if f.endswith(".csv")))
`,
      `import sys\nprint("mark", getattr(sys, "oc8_mark", None))`,
    ],
    undefined,
    { mounts: { 1: "top,secret\n" } },
  );
  expect((hook as Result).stdout).toBe("hooked\n");
  expect((mounted as Result).stdout).toBe("top,secret\n\n");
  expect((after as Result).stdout).toBe("leaked None\nfiles []\n");
  expect((mark as Result).stdout).toBe("mark None\n");
});

test("an import whose package never arrives is given up", async ({ page }) => {
  hang = /^numpy-/;
  const [stuck, after] = await run(page, ["import numpy", "print('next')"], undefined, {
    loadTimeoutMs: 5000,
  });
  expect(stuck).toBe("error: Loading Python and its packages took too long.");
  expect((after as Result).stdout).toBe("next\n");
});
