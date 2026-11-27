// Runs Python from an answer in the browser (spec §5.3, §9). Loaded on the
// first Run click only (a dynamic import), and nothing is fetched before it.
//
// This page owns a hidden sandboxed frame (the artifact sandbox's srcdoc with
// PYTHON_CSP, see frame-runtime.ts) whose worker runs Pyodide. It fetches the
// vendored files from its own origin and hands them over as transferred
// ArrayBuffers; the frame never learns a URL of this origin. What the frame
// sends back is untrusted: messages from anywhere else are dropped, and the
// output is checked and capped (readOutput).
//
// Stop (the signal) and the 30 s wall clock terminate the worker; the next run
// starts a new one from the files the frame kept.

import { buildSandboxSrcdoc } from "../artifacts/sandbox-srcdoc";
import { CORE_FILES, FRAME_RUNTIME, PYTHON_CSP } from "./frame-runtime";
import {
  LIMITS,
  PythonNotInstalled,
  readOutput,
  type PythonFile,
  type PythonResult,
} from "./output";

export type { PythonFile, PythonResult } from "./output";

export type Progress = { loaded: number; total: number | null };

export type RunOptions = {
  /** Chat uploads to offer the code, read-only in its working directory.
   *  Their buffers are transferred (and detached here). */
  files?: PythonFile[];
  signal: AbortSignal;
  /** The download of Pyodide and packages. */
  onProgress?: (progress: Progress) => void;
  /** The code itself starts (packages are loaded). */
  onStarted?: () => void;
  /** Wall clock for the code; LIMITS.timeoutMs unless a test needs less. */
  timeoutMs?: number;
};

/** The frame as the host sees it. */
export type FrameLink = {
  post: (message: unknown, transfer: Transferable[]) => void;
  remove: () => void;
};

export type HostDeps = {
  /** A vendored file's bytes (`name` is checked against the lock file). */
  fetchAsset: (
    name: string,
    onBytes: (loaded: number, total: number | null) => void,
  ) => Promise<ArrayBuffer>;
  /** Mounts the frame with `srcdoc`; `onMessage` gets only its messages. */
  mountFrame: (srcdoc: string, onMessage: (data: unknown) => void) => FrameLink;
};

/** File names the frame may ask for: what the lock file lists. */
const FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._+-]*$/;

function abortError(): DOMException {
  return new DOMException("The run was stopped.", "AbortError");
}

function lockFileNames(bytes: ArrayBuffer): Set<string> {
  const names = new Set<string>();
  try {
    const lock = JSON.parse(new TextDecoder().decode(bytes)) as {
      packages?: Record<string, { file_name?: unknown }>;
    };
    for (const entry of Object.values(lock.packages ?? {})) {
      if (typeof entry.file_name === "string" && FILE_NAME.test(entry.file_name)) {
        names.add(entry.file_name);
      }
    }
  } catch {
    // An unreadable lock file offers no packages; the core still runs.
  }
  return names;
}

type Pending = {
  resolve: (result: PythonResult) => void;
  reject: (error: Error) => void;
  onStarted?: () => void;
  timeoutMs: number;
  timer?: ReturnType<typeof setTimeout>;
};

export function createPythonHost(deps: HostDeps) {
  let frame: FrameLink | null = null;
  let booted: Promise<void> | null = null;
  let allowed = new Set<string>();
  const inFlight = new Map<string, Promise<void>>();
  const pending = new Map<number, Pending>();
  let seq = 0;
  let queue: Promise<unknown> = Promise.resolve();
  let readyWaiter: (() => void) | null = null;
  let progressTo: ((p: Progress) => void) | undefined;
  const sizes = new Map<string, { loaded: number; total: number | null }>();

  function report(name: string, loaded: number, total: number | null) {
    sizes.set(name, { loaded, total });
    let sumLoaded = 0;
    let sumTotal = 0;
    let known = true;
    for (const s of sizes.values()) {
      sumLoaded += s.loaded;
      if (s.total === null) known = false;
      else sumTotal += s.total;
    }
    progressTo?.({ loaded: sumLoaded, total: known ? sumTotal : null });
  }

  function settle(id: number, how: (p: Pending) => void) {
    const p = pending.get(id);
    if (!p) return;
    pending.delete(id);
    if (p.timer) clearTimeout(p.timer);
    how(p);
  }

  function stopWorker() {
    frame?.post({ type: "stop" }, []);
  }

  async function provide(names: string[]) {
    for (const name of names) {
      if (!allowed.has(name) || inFlight.has(name)) continue;
      const job = deps
        .fetchAsset(name, (loaded, total) => report(name, loaded, total))
        .then(
          (bytes) => frame?.post({ type: "files", files: { [name]: bytes } }, [bytes]),
          () => frame?.post({ type: "files", files: {}, missing: [name] }, []),
        )
        .finally(() => inFlight.delete(name));
      inFlight.set(name, job);
    }
  }

  function receive(data: unknown) {
    if (!data || typeof data !== "object") return;
    const m = data as {
      type?: unknown;
      id?: unknown;
      names?: unknown;
      out?: unknown;
      error?: unknown;
    };
    if (m.type === "ready") {
      readyWaiter?.();
      return;
    }
    if (m.type === "need") {
      if (!Array.isArray(m.names)) return;
      const names = m.names.filter((n): n is string => typeof n === "string").slice(0, 64);
      const refused = names.filter((n) => !allowed.has(n));
      if (refused.length) frame?.post({ type: "files", files: {}, missing: refused }, []);
      void provide(names);
      return;
    }
    if (typeof m.id !== "number") return;
    const id = m.id;
    if (m.type === "started") {
      const p = pending.get(id);
      if (!p || p.timer) return;
      p.onStarted?.();
      p.timer = setTimeout(() => {
        stopWorker();
        settle(id, (q) => q.resolve(readOutput({}, true)));
      }, p.timeoutMs);
    } else if (m.type === "done") {
      settle(id, (p) => p.resolve(readOutput(m.out)));
    } else if (m.type === "failed") {
      const error = typeof m.error === "string" ? m.error.slice(0, 2000) : "";
      settle(id, (p) => p.reject(new Error(error || "Python could not run.")));
    }
  }

  async function boot(): Promise<void> {
    const ready = new Promise<void>((resolve) => {
      readyWaiter = resolve;
    });
    frame = deps.mountFrame(
      buildSandboxSrcdoc(`<script>${FRAME_RUNTIME}</script>`, PYTHON_CSP),
      receive,
    );
    const core = await Promise.all(
      CORE_FILES.map((name) =>
        deps.fetchAsset(name, (loaded, total) => report(name, loaded, total)),
      ),
    );
    const files: Record<string, ArrayBuffer> = {};
    CORE_FILES.forEach((name, i) => {
      files[name] = core[i];
    });
    allowed = lockFileNames(files["pyodide-lock.json"]);
    await ready;
    frame.post({ type: "boot", files }, core);
  }

  function ensureBooted(): Promise<void> {
    if (!booted) {
      booted = boot().catch((error: unknown) => {
        // A failed first download can be tried again.
        frame?.remove();
        frame = null;
        booted = null;
        throw error;
      });
    }
    return booted;
  }

  async function runNow(code: string, opts: RunOptions): Promise<PythonResult> {
    if (opts.signal.aborted) throw abortError();
    progressTo = opts.onProgress;
    const aborted = new Promise<never>((_, reject) => {
      opts.signal.addEventListener("abort", () => reject(abortError()), { once: true });
    });
    aborted.catch(() => undefined);
    await Promise.race([ensureBooted(), aborted]);
    const id = (seq += 1);
    const files = opts.files ?? [];
    const result = new Promise<PythonResult>((resolve, reject) => {
      pending.set(id, {
        resolve,
        reject,
        onStarted: opts.onStarted,
        timeoutMs: opts.timeoutMs ?? LIMITS.timeoutMs,
      });
    });
    opts.signal.addEventListener(
      "abort",
      () => {
        if (!pending.has(id)) return;
        stopWorker();
        settle(id, (p) => p.reject(abortError()));
      },
      { once: true },
    );
    frame?.post(
      {
        type: "run",
        id,
        code,
        mounts: files.map((f) => ({ name: f.name, bytes: f.bytes })),
      },
      files.map((f) => f.bytes),
    );
    try {
      return await result;
    } finally {
      progressTo = undefined;
    }
  }

  return {
    /** Runs `code`; one run at a time, in order. */
    run(code: string, opts: RunOptions): Promise<PythonResult> {
      const next = queue.then(() => runNow(code, opts));
      queue = next.catch(() => undefined);
      return next;
    },
  };
}

/** The app's own copy of a vendored file, with download progress. A 404
 *  means the server has no Pyodide (the folder is left out of its image). */
export async function fetchVendored(
  name: string,
  onBytes: (loaded: number, total: number | null) => void,
): Promise<ArrayBuffer> {
  const res = await fetch(`/pyodide/${encodeURIComponent(name)}`, { credentials: "omit" });
  if (res.status === 404) throw new PythonNotInstalled();
  if (!res.ok) throw new Error(`/pyodide/${name}: ${res.status}`);
  const length = Number(res.headers.get("content-length"));
  const total = Number.isFinite(length) && length > 0 ? length : null;
  if (!res.body) {
    const bytes = await res.arrayBuffer();
    onBytes(bytes.byteLength, bytes.byteLength);
    return bytes;
  }
  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let loaded = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    loaded += value.byteLength;
    onBytes(loaded, total);
  }
  const out = new Uint8Array(loaded);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.byteLength;
  }
  onBytes(loaded, loaded);
  return out.buffer;
}

/** A hidden frame in this page; only its own window's messages count. */
function mountHiddenFrame(srcdoc: string, onMessage: (data: unknown) => void): FrameLink {
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.setAttribute("aria-hidden", "true");
  frame.tabIndex = -1;
  frame.title = "Python";
  frame.hidden = true;
  const receive = (event: MessageEvent) => {
    if (!frame.contentWindow || event.source !== frame.contentWindow) return;
    onMessage(event.data);
  };
  window.addEventListener("message", receive);
  frame.srcdoc = srcdoc;
  document.body.append(frame);
  return {
    post: (message, transfer) => frame.contentWindow?.postMessage(message, "*", transfer),
    remove: () => {
      window.removeEventListener("message", receive);
      frame.remove();
    },
  };
}

let host: ReturnType<typeof createPythonHost> | null = null;

/** Runs `code` in the page's Python sandbox (created on the first call). */
export function runPython(code: string, opts: RunOptions): Promise<PythonResult> {
  host ??= createPythonHost({ fetchAsset: fetchVendored, mountFrame: mountHiddenFrame });
  return host.run(code, opts);
}
