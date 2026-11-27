// The Run Python host against a fake frame (spec §5.3, §9): what it fetches,
// what it hands the frame, the wall clock, Stop, and what it accepts back.
// The real frame, worker and Pyodide in Chromium and Firefox, with a counted
// outside server: e2e/python-sandbox.spec.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sandboxCsp } from "@/components/conversation/artifacts/sandbox-srcdoc";
import {
  CORE_FILES,
  FRAME_RUNTIME,
  PYTHON_CSP,
  WORKER_SOURCE,
} from "@/components/conversation/python/frame-runtime";
import {
  LIMITS,
  PythonNotInstalled,
  readOutput,
  resultMessage,
} from "@/components/conversation/python/output";
import {
  createPythonHost,
  fetchVendored,
  runPython,
  type HostDeps,
} from "@/components/conversation/python/runner";
import { resetPython } from "@/lib/python-reset";

/** A PNG's first bytes (signature and IHDR) as a data URL. */
function png(width: number, height: number): string {
  const bytes = new Uint8Array(33);
  bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]);
  const view = new DataView(bytes.buffer);
  view.setUint32(16, width);
  view.setUint32(20, height);
  bytes.set([8, 6, 0, 0, 0], 24);
  return `data:image/png;base64,${btoa(String.fromCharCode(...bytes))}`;
}

const LOCK = JSON.stringify({
  packages: {
    numpy: { file_name: "numpy-2.2.5-cp313-cp313-pyemscripten_2025_0_wasm32.whl" },
    evil: { file_name: "../../api/v1/auth/me" },
  },
});

type Posted = { message: Record<string, unknown>; transfer: Transferable[] };

function fakeFrame() {
  const posted: Posted[] = [];
  const fetched: string[] = [];
  let srcdoc = "";
  let say: (data: unknown) => void = () => undefined;
  const deps: HostDeps = {
    fetchAsset: vi.fn(async (name: string, onBytes) => {
      fetched.push(name);
      const bytes =
        name === "pyodide-lock.json" ? new TextEncoder().encode(LOCK) : new Uint8Array(8);
      onBytes(bytes.byteLength, bytes.byteLength);
      return bytes.buffer as ArrayBuffer;
    }),
    mountFrame: vi.fn((doc: string, onMessage: (data: unknown) => void) => {
      srcdoc = doc;
      say = onMessage;
      queueMicrotask(() => onMessage({ type: "ready" }));
      return {
        post: (message: unknown, transfer: Transferable[]) =>
          posted.push({ message: message as Record<string, unknown>, transfer }),
        remove: vi.fn(),
      };
    }),
  };
  const lastRun = () => [...posted].reverse().find((p) => p.message.type === "run");
  return {
    deps,
    posted,
    fetched,
    srcdoc: () => srcdoc,
    say: (data: unknown) => say(data),
    lastRun,
    /** Waits until the host has posted a run. */
    async untilRun() {
      await vi.waitFor(() => expect(lastRun()).toBeDefined());
      return lastRun()!.message.id as number;
    },
  };
}

describe("the Python host", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads nothing until the first run", () => {
    const frame = fakeFrame();
    createPythonHost(frame.deps);
    expect(frame.deps.fetchAsset).not.toHaveBeenCalled();
    expect(frame.deps.mountFrame).not.toHaveBeenCalled();
  });

  it("fetches the core files itself and transfers their bytes into the frame", async () => {
    const frame = fakeFrame();
    const host = createPythonHost(frame.deps);
    const run = host.run("print(1)", { signal: new AbortController().signal });
    const id = await frame.untilRun();
    expect(frame.fetched).toEqual([...CORE_FILES]);
    const boot = frame.posted.find((p) => p.message.type === "boot")!;
    const files = boot.message.files as Record<string, ArrayBuffer>;
    expect(Object.keys(files)).toEqual([...CORE_FILES]);
    // Every buffer is transferred, not copied.
    expect(boot.transfer).toHaveLength(CORE_FILES.length);
    for (const name of CORE_FILES) expect(boot.transfer).toContain(files[name]);
    frame.say({ type: "done", id, out: { stdout: "1\n" } });
    await expect(run).resolves.toMatchObject({ stdout: "1\n", timedOut: false });
  });

  it("never gives the frame a URL of this origin", async () => {
    const frame = fakeFrame();
    const host = createPythonHost(frame.deps);
    void host.run("x", { signal: new AbortController().signal });
    await frame.untilRun();
    const said = JSON.stringify(frame.posted.map((p) => p.message)) + frame.srcdoc();
    expect(said).not.toContain(window.location.origin);
    // No path of this app (the worker's own "/home/pyodide/" is its file system).
    expect(said).not.toMatch(/["'`]\/pyodide\//);
    expect(said).not.toContain("/api/");
  });

  it("builds the frame from the artifact sandbox with no network directive", async () => {
    const frame = fakeFrame();
    void createPythonHost(frame.deps).run("x", { signal: new AbortController().signal });
    await frame.untilRun();
    const doc = frame.srcdoc();
    expect(doc).toContain('<meta http-equiv="Content-Security-Policy"');
    const csp = sandboxCsp(PYTHON_CSP);
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("worker-src blob:");
    expect(csp).not.toMatch(/connect-src/);
    expect(PYTHON_CSP).not.toMatch(/connect-src|https?:|'self'/);
    // The runtime is safe inside a <script> element.
    expect(FRAME_RUNTIME).not.toMatch(/<\/|<!--/);
    // The worker's fetch serves only the in-memory files.
    expect(WORKER_SOURCE).toContain('everywhere("fetch", localFetch)');
    expect(WORKER_SOURCE).toContain('everywhere("postMessage", undefined)');
  });

  it("fetches only package files the lock file names, and transfers them", async () => {
    const frame = fakeFrame();
    void createPythonHost(frame.deps).run("import numpy", { signal: new AbortController().signal });
    await frame.untilRun();
    const wheel = "numpy-2.2.5-cp313-cp313-pyemscripten_2025_0_wasm32.whl";
    frame.say({ type: "need", names: [wheel, "../../api/v1/auth/me", "secrets.txt", 7] });
    await vi.waitFor(() =>
      expect(
        frame.posted.some((p) =>
          Object.hasOwn((p.message.files as object | undefined) ?? {}, wheel),
        ),
      ).toBe(true),
    );
    expect(frame.fetched).toContain(wheel);
    expect(frame.fetched).not.toContain("secrets.txt");
    expect(frame.fetched).not.toContain("../../api/v1/auth/me");
    const sent = frame.posted.find((p) =>
      Object.hasOwn((p.message.files as object | undefined) ?? {}, wheel),
    )!;
    expect(sent.transfer).toHaveLength(1);
    const refused = frame.posted.find((p) => Array.isArray(p.message.missing))!;
    expect(refused.message.missing).toEqual(["../../api/v1/auth/me", "secrets.txt"]);
  });

  it("stops the worker after 30 s of running and says so", async () => {
    const frame = fakeFrame();
    const host = createPythonHost(frame.deps);
    const run = host.run("while True: pass", { signal: new AbortController().signal });
    const id = await frame.untilRun();
    vi.useFakeTimers();
    frame.say({ type: "started", id });
    vi.advanceTimersByTime(LIMITS.timeoutMs - 1);
    expect(frame.posted.some((p) => p.message.type === "stop")).toBe(false);
    vi.advanceTimersByTime(1);
    expect(frame.posted.some((p) => p.message.type === "stop")).toBe(true);
    vi.useRealTimers();
    await expect(run).resolves.toMatchObject({ timedOut: true, stdout: "" });
    // A late answer of the stopped run changes nothing.
    frame.say({ type: "done", id, out: { stdout: "late" } });
  });

  it("Stop aborts the run and terminates the worker", async () => {
    const frame = fakeFrame();
    const host = createPythonHost(frame.deps);
    const stop = new AbortController();
    const run = host.run("x", { signal: stop.signal });
    const id = await frame.untilRun();
    frame.say({ type: "started", id });
    stop.abort();
    await expect(run).rejects.toMatchObject({ name: "AbortError" });
    expect(frame.posted.at(-1)?.message).toEqual({ type: "stop" });
    // The next run goes to the same frame (a new worker there).
    const again = host.run("print(2)", { signal: new AbortController().signal });
    await vi.waitFor(() => expect(frame.lastRun()?.message.id).toBe(id + 1));
    frame.say({ type: "done", id: id + 1, out: { stdout: "2\n" } });
    await expect(again).resolves.toMatchObject({ stdout: "2\n" });
    expect(frame.deps.mountFrame).toHaveBeenCalledTimes(1);
  });

  it("transfers mounted chat files", async () => {
    const frame = fakeFrame();
    const bytes = new TextEncoder().encode("a,b\n").buffer as ArrayBuffer;
    void createPythonHost(frame.deps).run("x", {
      signal: new AbortController().signal,
      files: [{ name: "sales.csv", bytes }],
    });
    await frame.untilRun();
    const run = frame.lastRun()!;
    expect(run.message.mounts).toEqual([{ name: "sales.csv", bytes }]);
    expect(run.transfer).toEqual([bytes]);
  });

  it("gives up on a run whose packages do not load within the load deadline", async () => {
    const frame = fakeFrame();
    const run = createPythonHost(frame.deps).run("import numpy", {
      signal: new AbortController().signal,
      loadTimeoutMs: 300,
    });
    await frame.untilRun();
    expect(frame.posted.some((p) => p.message.type === "stop")).toBe(false);
    await expect(run).rejects.toThrow("Loading Python and its packages took too long.");
    expect(frame.posted.at(-1)?.message).toEqual({ type: "stop" });
  });

  it("the load deadline is 120 s and separate from the 30 s clock", async () => {
    expect(LIMITS.loadTimeoutMs).toBe(120_000);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const frame = fakeFrame();
    const run = createPythonHost(frame.deps).run("x", { signal: new AbortController().signal });
    const id = await frame.untilRun();
    vi.advanceTimersByTime(100_000);
    frame.say({ type: "started", id });
    // The load deadline no longer counts once the code started.
    vi.advanceTimersByTime(25_000);
    expect(frame.posted.some((p) => p.message.type === "stop")).toBe(false);
    vi.advanceTimersByTime(5_000);
    vi.useRealTimers();
    await expect(run).resolves.toMatchObject({ timedOut: true });
  });

  it("dispose rejects the run in progress and removes the frame", async () => {
    const frame = fakeFrame();
    const host = createPythonHost(frame.deps);
    const run = host.run("x", { signal: new AbortController().signal });
    await frame.untilRun();
    host.dispose();
    await expect(run).rejects.toMatchObject({ name: "AbortError" });
    const link = (frame.deps.mountFrame as ReturnType<typeof vi.fn>).mock.results[0].value;
    expect(link.remove).toHaveBeenCalledTimes(1);
  });

  it("rejects a failed run with the frame's message, capped", async () => {
    const frame = fakeFrame();
    const run = createPythonHost(frame.deps).run("x", { signal: new AbortController().signal });
    const id = await frame.untilRun();
    frame.say({ type: "failed", id, error: "x".repeat(5000) });
    await expect(run).rejects.toThrow(/^x{2000}$/);
  });
});

describe("fetchVendored", () => {
  afterEach(() => vi.restoreAllMocks());

  it("says Python is not installed when the server has no Pyodide (404)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("", { status: 404 }));
    await expect(fetchVendored("pyodide.js", () => undefined)).rejects.toBeInstanceOf(
      PythonNotInstalled,
    );
  });

  it("reads the app's own copy without credentials and reports progress", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(new Uint8Array([1, 2, 3]), { status: 200 }));
    const seen: number[] = [];
    const bytes = await fetchVendored("pyodide.js", (loaded) => seen.push(loaded));
    expect(new Uint8Array(bytes)).toEqual(new Uint8Array([1, 2, 3]));
    expect(spy).toHaveBeenCalledWith(
      "/pyodide/pyodide.js",
      expect.objectContaining({ credentials: "omit", signal: expect.any(AbortSignal) }),
    );
    expect(seen.at(-1)).toBe(3);
  });

  it("gives up on a download that never answers, so a later run can try again", async () => {
    let signal: AbortSignal | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => {
      signal = init?.signal ?? undefined;
      return new Promise<Response>(() => undefined);
    });
    await expect(fetchVendored("pyodide.js", () => undefined, 20)).rejects.toThrow(/stalled/);
    expect(signal?.aborted).toBe(true);
  });

  it("gives up on a download whose bytes stop coming", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([1, 2]));
        // ...and nothing more, ever.
      },
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { status: 200 }));
    const seen: number[] = [];
    await expect(
      fetchVendored("pyodide.asm.wasm", (loaded) => seen.push(loaded), 20),
    ).rejects.toThrow(/stalled/);
    expect(seen).toEqual([2]);
  });

  it("says Python is not installed when the server answers with its HTML page", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("<!doctype html><title>oc8</title>", {
        status: 200,
        headers: { "content-type": "text/html; charset=utf-8" },
      }),
    );
    await expect(fetchVendored("pyodide.asm.wasm", () => undefined)).rejects.toBeInstanceOf(
      PythonNotInstalled,
    );
  });

  it("a missing first download can be tried again", async () => {
    let calls = 0;
    const deps: HostDeps = {
      fetchAsset: async () => {
        calls += 1;
        throw new PythonNotInstalled();
      },
      mountFrame: () => ({ post: () => undefined, remove: () => undefined }),
    };
    const host = createPythonHost(deps);
    await expect(host.run("1", { signal: new AbortController().signal })).rejects.toBeInstanceOf(
      PythonNotInstalled,
    );
    await expect(host.run("1", { signal: new AbortController().signal })).rejects.toBeInstanceOf(
      PythonNotInstalled,
    );
    expect(calls).toBeGreaterThan(CORE_FILES.length);
  });
});

describe("resetPython", () => {
  afterEach(() => vi.restoreAllMocks());

  it("takes the page's Python frame away and stops its run", async () => {
    // The download never finishes: the run is still booting.
    vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise(() => undefined));
    const run = runPython("x", { signal: new AbortController().signal });
    await vi.waitFor(() => expect(document.querySelector("iframe[title=Python]")).not.toBeNull());
    resetPython();
    await expect(run).rejects.toMatchObject({ name: "AbortError" });
    expect(document.querySelector("iframe[title=Python]")).toBeNull();
    // The next run starts over with a new frame.
    void runPython("y", { signal: new AbortController().signal }).catch(() => undefined);
    await vi.waitFor(() =>
      expect(document.querySelectorAll("iframe[title=Python]")).toHaveLength(1),
    );
    resetPython();
  });
});

describe("readOutput", () => {
  it("caps text, images and tables", () => {
    const image = png(64, 48);
    const out = readOutput({
      stdout: "a".repeat(LIMITS.text + 50),
      stderr: 5,
      result: "r",
      images: Array.from({ length: 15 }, () => image),
      tables: [
        {
          columns: ["n", 2],
          rows: Array.from({ length: 500 }, (_, i) => [String(i), { x: 1 }, "extra"]),
        },
      ],
    });
    expect(out.stdout.length).toBeLessThanOrEqual(LIMITS.text + 2);
    expect(out.stderr).toBe("");
    expect(out.result).toBe("r");
    expect(out.images).toHaveLength(LIMITS.images);
    expect(out.tables[0].columns).toEqual(["n", ""]);
    expect(out.tables[0].rows).toHaveLength(LIMITS.rows);
    expect(out.tables[0].rows[0]).toEqual(["0", ""]);
  });

  it("keeps only PNG data URLs of plain base64", () => {
    const out = readOutput({
      images: [
        "data:image/svg+xml;base64,PHN2Zz4=",
        "javascript:alert(1)",
        'data:image/png;base64,abc"onerror=alert(1)',
        "https://example.com/x.png",
        `data:image/png;base64,${"A".repeat(LIMITS.imageChars)}`,
        png(64, 48),
      ],
    });
    expect(out.images).toEqual([png(64, 48)]);
  });

  it("refuses a PNG over 4096 x 4096 or without a PNG header", () => {
    const out = readOutput({
      images: [png(4097, 10), png(10, 5000), "data:image/png;base64,AAAA", png(4096, 4096)],
    });
    expect(out.images).toEqual([png(4096, 4096)]);
  });

  it("keeps the whole output within one budget", () => {
    const big = "a".repeat(LIMITS.text);
    const cells = Array.from({ length: 200 }, () => Array.from({ length: 100 }, () => "x"));
    const out = readOutput({
      stdout: big,
      stderr: big,
      result: big,
      images: Array.from({ length: 10 }, () => png(64, 48) + "A".repeat(1_000_000)),
      tables: Array.from({ length: 10 }, () => ({ columns: Array(100).fill("c"), rows: cells })),
    });
    const tableCells = out.tables.reduce((n, t) => n + t.rows.length * t.columns.length, 0);
    expect(tableCells).toBeLessThanOrEqual(LIMITS.tableCells);
    const total =
      out.stdout.length +
      out.stderr.length +
      (out.result?.length ?? 0) +
      out.images.reduce((n, i) => n + i.length, 0) +
      out.tables.reduce((n, t) => n + [t.columns, ...t.rows].flat().join("").length, 0);
    expect(total).toBeLessThanOrEqual(LIMITS.total + 10);
  });

  it("reads junk as empty output", () => {
    expect(readOutput("junk")).toEqual({
      stdout: "",
      stderr: "",
      result: null,
      images: [],
      tables: [],
      timedOut: false,
    });
  });
});

describe("resultMessage", () => {
  it("is at most 4 000 characters and keeps the fence closed", () => {
    const text = resultMessage(
      { ...readOutput({}), stdout: "x".repeat(10_000) + "```" },
      "Result:",
    );
    expect(text.length).toBeLessThanOrEqual(LIMITS.message);
    expect(text.startsWith("Result:\n\n```text\n")).toBe(true);
    expect(text.endsWith("\n```")).toBe(true);
    expect(text.match(/```/g)).toHaveLength(2);
  });

  it("joins output, last value and tables; empty when nothing ran", () => {
    const text = resultMessage(
      {
        ...readOutput({}),
        stdout: "hi\n",
        result: "42",
        tables: [{ columns: ["a"], rows: [["1"]] }],
      },
      "R",
    );
    expect(text).toBe("R\n\n```text\nhi\n\n42\n\na\n1\n```");
    expect(resultMessage(readOutput({}), "R")).toBe("");
  });
});
