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
import { LIMITS, readOutput, resultMessage } from "@/components/conversation/python/output";
import { createPythonHost, type HostDeps } from "@/components/conversation/python/runner";

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

  it("rejects a failed run with the frame's message, capped", async () => {
    const frame = fakeFrame();
    const run = createPythonHost(frame.deps).run("x", { signal: new AbortController().signal });
    const id = await frame.untilRun();
    frame.say({ type: "failed", id, error: "x".repeat(5000) });
    await expect(run).rejects.toThrow(/^x{2000}$/);
  });
});

describe("readOutput", () => {
  it("caps text, images and tables", () => {
    const png = "data:image/png;base64,iVBORw0KGgo=";
    const out = readOutput({
      stdout: "a".repeat(LIMITS.text + 50),
      stderr: 5,
      result: "r",
      images: Array.from({ length: 15 }, () => png),
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
        "data:image/png;base64,iVBORw0KGgo=",
      ],
    });
    expect(out.images).toEqual(["data:image/png;base64,iVBORw0KGgo="]);
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
