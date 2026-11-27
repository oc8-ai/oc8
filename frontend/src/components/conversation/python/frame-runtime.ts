// The code that runs Python for "Run" in the chat (spec §5.3, §9), as source
// text for the sandboxed frame. It is not part of the app's realm:
//
//   app page (runner.ts) -- postMessage, ArrayBuffers --> frame (FRAME_RUNTIME)
//     frame: an opaque-origin srcdoc (`sandbox="allow-scripts"`, the artifact
//     sandbox's CSP and GUARD) that keeps the files as Blobs and starts
//   --> a Worker from a blob: URL (WORKER_SOURCE), where Pyodide runs.
//
// No step has a network path: the frame's CSP has no connect-src (default-src
// 'none'; a blob: worker inherits it), and the worker's `fetch` is replaced
// before Pyodide loads by one that serves only the files the app page handed
// in, under a base URL that is never resolved (PYODIDE_BASE). The app page
// fetches those files from its own origin (/pyodide/, vendored by
// scripts/vendor-pyodide.mjs) and only by names the lock file lists.
// The worker also loses every other way to send (XHR, WebSocket, WebRTC,
// EventSource, WebTransport), to load more script (importScripts) and to
// start a fresh realm (Worker), and its postMessage once the protocol has
// captured it -- in a closure, not on the global, so Python cannot reach it
// through `js` or `js.eval`. If any of that cannot be done, the worker fails
// to start instead of running unhardened. Python code can neither reach the
// network nor talk to the frame except through the run's result.
//
// One worker per run: the frame starts it for the run and terminates it when
// the run ends, so nothing a run leaves behind reaches the next one.

/** Where Pyodide believes its files are. `.invalid` never resolves, and the
 *  worker's fetch serves this prefix from memory only. */
export const PYODIDE_BASE = "https://pyodide.invalid/";

/** What the app page fetches before the first run. */
export const CORE_FILES = [
  "pyodide.js",
  "pyodide.asm.js",
  "pyodide.asm.wasm",
  "python_stdlib.zip",
  "pyodide-lock.json",
] as const;

/** `extraCsp` for the frame (see sandboxCsp): WebAssembly, Pyodide's eval,
 *  scripts and the worker from blob: URLs. No connect-src -- sandboxCsp
 *  would drop one anyway. */
export const PYTHON_CSP =
  "script-src 'unsafe-inline' 'wasm-unsafe-eval' 'unsafe-eval' blob:; worker-src blob:";

/** Collects what a run shows besides stdout/stderr: figures as PNG, a
 *  DataFrame/Series it ended with as a table, else the last value's repr.
 *  Caps match LIMITS in output.ts (the app page checks them again). */
const PY_HELPER = `
import base64, io, json, sys, traceback, warnings
from pyodide.code import eval_code_async

warnings.filterwarnings("ignore", message=".*non-interactive.*")
warnings.filterwarnings("ignore", message=".*FigureCanvasAgg.*")

MAX_IMAGES, MAX_ROWS, MAX_COLUMNS, MAX_CELL, MAX_RESULT = 10, 200, 100, 1000, 100000


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit] + "\\u2026"


def _table(frame, pd):
    if not isinstance(frame.index, pd.RangeIndex):
        frame = frame.reset_index()
    frame = frame.iloc[:MAX_ROWS, :MAX_COLUMNS]
    return {
        "columns": [_cut(str(c), MAX_CELL) for c in frame.columns],
        "rows": [[_cut(str(v), MAX_CELL) for v in row] for row in frame.itertuples(index=False, name=None)],
    }


def _collect(value):
    images, tables, result = [], [], None
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is not None:
        for number in plt.get_fignums()[:MAX_IMAGES]:
            buffer = io.BytesIO()
            plt.figure(number).savefig(buffer, format="png", dpi=100, bbox_inches="tight")
            images.append("data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
        plt.close("all")
    pd = sys.modules.get("pandas")
    if value is not None:
        if pd is not None and isinstance(value, pd.DataFrame):
            tables.append(_table(value, pd))
        elif pd is not None and isinstance(value, pd.Series):
            tables.append(_table(value.to_frame(), pd))
        else:
            result = _cut(repr(value), MAX_RESULT)
    return {"images": images, "tables": tables, "result": result}


def preload(code, names):
    """Imports the vendored packages \`code\` imports, before the wall clock
    starts: compiling their native modules can take seconds (Firefox)."""
    import importlib
    from pyodide.code import find_imports

    allowed = set(names)
    for name in find_imports(code):
        top = name.split(".")[0]
        if top in allowed:
            try:
                importlib.import_module(top)
                if top == "matplotlib":
                    importlib.import_module("matplotlib.pyplot")
            except BaseException:
                pass  # the code's own import reports it


async def run(code, namespace):
    value, error = None, None
    try:
        value = await eval_code_async(code, namespace, filename="<answer>")
    except BaseException as exc:
        tb = exc.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename != "<answer>":
            tb = tb.tb_next
        error = "".join(traceback.format_exception(type(exc), exc, tb))
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        out = _collect(value)
    except BaseException as exc:
        out = {"images": [], "tables": [], "result": None}
        error = (error or "") + "".join(traceback.format_exception_only(type(exc), exc))
    out["error"] = error
    return json.dumps(out)
`;

const WORKER_BODY = `const scope = self;
const send = scope.postMessage.bind(scope);
const listen = scope.addEventListener.bind(scope);
const Resp = Response;
const MAX_TEXT = 100000;

// Files by name (Blobs from the frame) and the fetches waiting for one.
const files = new Map();
const waiting = new Map();
const blobFor = (name) => {
  if (files.has(name)) return Promise.resolve(files.get(name));
  return new Promise((resolve, reject) => {
    const list = waiting.get(name);
    if (list) list.push({ resolve, reject });
    else {
      waiting.set(name, [{ resolve, reject }]);
      send({ type: "need", names: [name] });
    }
  });
};
const NAME = /^[A-Za-z0-9][A-Za-z0-9._+-]*$/;
const offline = () => Promise.reject(new TypeError("There is no network in this sandbox."));
function localFetch(input) {
  let url = "";
  try { url = String(input && typeof input === "object" && "url" in input ? input.url : input); } catch (e) { return offline(); }
  if (url.indexOf(BASE) !== 0) return offline();
  const name = url.slice(BASE.length).split(/[?#]/)[0];
  if (!NAME.test(name)) return offline();
  return blobFor(name).then((blob) => new Resp(blob, {
    status: 200,
    headers: { "content-type": /[.]wasm$/.test(name) ? "application/wasm" : "application/octet-stream" },
  }));
}

// Every way to send or to open a fresh realm goes, on the global and along
// its prototype chain, non-writable and non-configurable.
const lock = (target, name, value) => {
  try { Object.defineProperty(target, name, { value, writable: false, configurable: false, enumerable: false }); } catch (e) {}
  // Already locked by the browser is fine only if it already is what we want.
  if (target[name] !== value) throw new Error("The Python sandbox could not be set up (" + name + ").");
};
const everywhere = (name, value) => {
  for (let o = Object.getPrototypeOf(scope); o; o = Object.getPrototypeOf(o)) {
    if (Object.prototype.hasOwnProperty.call(o, name)) lock(o, name, value);
  }
  lock(scope, name, value);
};
for (const name of ["XMLHttpRequest", "WebSocket", "WebSocketStream", "WebTransport", "EventSource",
  "RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel", "RTCRtpScriptTransformer",
  "Worker", "SharedWorker", "BroadcastChannel", "importScripts"]) {
  everywhere(name, undefined);
}
everywhere("fetch", localFetch);
everywhere("postMessage", undefined);

let ready = null;
let stdout = "";
let stderr = "";
const add = (which, text) => {
  if (which === "out") { if (stdout.length < MAX_TEXT) stdout += text; }
  else if (stderr.length < MAX_TEXT) stderr += text;
};
const message = (error) => String((error && error.message) || error).slice(0, 4000);

function boot(core) {
  for (const name of Object.keys(core)) files.set(name, core[name]);
  ready = (async () => {
    // Evaluated, not importScripts(blob:): Firefox refuses a blob: script
    // in an opaque-origin worker. Both set their global on globalThis.
    const evaluate = eval;
    for (const name of ["pyodide.js", "pyodide.asm.js"]) evaluate(await files.get(name).text());
    const lockFile = JSON.parse(await files.get("pyodide-lock.json").text());
    const py = await loadPyodide({
      indexURL: BASE,
      packageBaseUrl: BASE,
      lockFileContents: lockFile,
      env: { MPLBACKEND: "Agg", HOME: "/home/pyodide" },
      stdout: (line) => add("out", line + "\\n"),
      stderr: (line) => add("err", line + "\\n"),
    });
    py.setStdin({ stdin: () => null });
    const helper = py.globals.get("dict")();
    py.runPython(HELPER, { globals: helper });
    // Top-level names of the vendored packages (not the code's own files).
    const packageImports = [];
    for (const entry of Object.values(lockFile.packages || {})) {
      for (const name of entry.imports || []) packageImports.push(String(name));
    }
    return { py, run: helper.get("run"), preload: helper.get("preload"), packageImports };
  })();
}

const safeName = (name, used) => {
  let base = String(name).split(/[\\\\/]/).pop().replace(/[^A-Za-z0-9._ -]/g, "_").slice(0, 120);
  if (!base || base === "." || base === "..") base = "file";
  let candidate = base;
  for (let i = 2; used.has(candidate); i += 1) candidate = i + "-" + base;
  used.add(candidate);
  return candidate;
};

async function run(m) {
  const id = m.id;
  try {
    const { py, run: runCode, preload, packageImports } = await ready;
    const used = new Set();
    for (const file of m.mounts || []) {
      const path = "/home/pyodide/" + safeName(file.name, used);
      py.FS.writeFile(path, new Uint8Array(file.bytes));
      py.FS.chmod(path, 0o444);
    }
    stdout = "";
    stderr = "";
    try {
      await py.loadPackagesFromImports(m.code, {
        messageCallback: () => undefined,
        errorCallback: (text) => add("err", text + "\\n"),
      });
    } catch (error) {
      add("err", message(error) + "\\n");
    }
    const names = py.toPy(packageImports);
    try { preload(m.code, names); } catch (error) {} finally { names.destroy(); }
    send({ type: "started", id });
    const namespace = py.globals.get("dict")();
    let out;
    try {
      out = JSON.parse(await runCode(m.code, namespace));
    } finally {
      namespace.destroy();
    }
    if (out.error) add("err", out.error);
    send({ type: "done", id, out: { stdout, stderr, result: out.result, images: out.images, tables: out.tables } });
  } catch (error) {
    send({ type: "failed", id, error: message(error) });
  }
}

listen("message", (event) => {
  const m = event.data;
  if (!m || typeof m !== "object") return;
  if (m.type === "boot") boot(m.files);
  else if (m.type === "files") {
    for (const name of Object.keys(m.files || {})) {
      files.set(name, m.files[name]);
      for (const w of waiting.get(name) || []) w.resolve(m.files[name]);
      waiting.delete(name);
    }
    for (const name of m.missing || []) {
      for (const w of waiting.get(name) || []) w.reject(new TypeError(name + " is not available offline."));
      waiting.delete(name);
    }
  } else if (m.type === "run") void run(m);
});
`;

/** The worker's source: Pyodide, the offline fetch and the run protocol. */
export const WORKER_SOURCE =
  `"use strict";\n(() => {\nconst BASE = ${JSON.stringify(PYODIDE_BASE)};\n` +
  `const HELPER = ${JSON.stringify(PY_HELPER)};\n` +
  WORKER_BODY +
  "\n})();\n";

const FRAME_BODY = `(() => {
  "use strict";
  const host = window.parent;
  const toHost = (message) => host.postMessage(message, "*");
  // The files the app page handed in, kept for the next worker.
  const blobs = new Map();
  const core = {};
  const workerUrl = URL.createObjectURL(new Blob([WORKER], { type: "text/javascript" }));
  let worker = null;
  let current = null;

  const stop = () => {
    if (worker) worker.terminate();
    worker = null;
  };
  const fromWorker = (m) => {
    if (!m || typeof m !== "object") return;
    if (m.type === "need" && Array.isArray(m.names)) {
      const have = {};
      const missing = [];
      for (const name of m.names) {
        if (typeof name !== "string") continue;
        if (blobs.has(name)) have[name] = blobs.get(name);
        else missing.push(name);
      }
      if (Object.keys(have).length && worker) worker.postMessage({ type: "files", files: have });
      if (missing.length) toHost({ type: "need", names: missing });
    } else if (m.type === "started") {
      toHost({ type: "started", id: m.id });
    } else if (m.type === "done" || m.type === "failed") {
      toHost({ type: m.type, id: m.id, out: m.out, error: m.error });
      // The run is over: its worker, and all it holds, go.
      stop();
    }
  };
  const start = () => {
    worker = new Worker(workerUrl);
    worker.onmessage = (event) => fromWorker(event.data);
    worker.onerror = (event) => {
      event.preventDefault();
      toHost({ type: "failed", id: current, error: "Python stopped: " + String(event.message || "") });
      stop();
    };
    worker.postMessage({ type: "boot", files: core });
  };
  const keep = (files) => {
    const kept = {};
    for (const name of Object.keys(files || {})) {
      const value = files[name];
      if (!(value instanceof ArrayBuffer)) continue;
      kept[name] = new Blob([value]);
      blobs.set(name, kept[name]);
    }
    return kept;
  };

  addEventListener("message", (event) => {
    if (event.source !== host) return;
    const m = event.data;
    if (!m || typeof m !== "object") return;
    if (m.type === "boot") Object.assign(core, keep(m.files));
    else if (m.type === "files") {
      const kept = keep(m.files);
      if (worker) worker.postMessage({ type: "files", files: kept, missing: Array.isArray(m.missing) ? m.missing : [] });
    } else if (m.type === "run") {
      current = m.id;
      stop();
      start();
      worker.postMessage({ type: "run", id: m.id, code: String(m.code), mounts: Array.isArray(m.mounts) ? m.mounts : [] });
    } else if (m.type === "stop") stop();
  });
  toHost({ type: "ready" });
})();`;

/** The frame's script. Safe inside a <script> element: no closing tag or
 *  comment opener survives the escaping. */
export const FRAME_RUNTIME = (
  `const WORKER = ${JSON.stringify(WORKER_SOURCE)};\n` + FRAME_BODY
).replace(/<(\/|!--)/g, "\\x3c$1");
