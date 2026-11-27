// Run under a Python block (spec §5.3, §9): offered only where an answer's
// previews may run scripts, nothing loaded before the first click, output
// shown as text, "Send result to chat" through the conversation's send path,
// chat uploads mounted on request.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("shiki", () => ({
  codeToHtml: async (code: string) =>
    `<pre class="shiki"><code>${code.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</code></pre>`,
}));
const previews = vi.hoisted(() => ({
  value: { data: { runScripts: true }, isError: false } as {
    data?: { runScripts: boolean };
    isError: boolean;
  },
}));
vi.mock("@/lib/preview-settings", () => ({ usePreviewSettings: () => previews.value }));
vi.mock("@/lib/record-links", async (orig) => ({
  ...(await orig<typeof import("@/lib/record-links")>()),
  useVerifiedLinks: () => ({ data: undefined }),
}));
const runner = vi.hoisted(() => ({ imported: false, runPython: vi.fn() }));
vi.mock("@/components/conversation/python/runner", () => {
  runner.imported = true;
  return { runPython: runner.runPython };
});
const download = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  downloadFileAttachment: download,
}));

import { ChatMarkdown } from "@/components/chat-markdown";
import { readOutput, LIMITS, type PythonResult } from "@/components/conversation/python/output";
import {
  PythonRunContext,
  usePythonScopeFor,
  type PythonScope,
} from "@/components/conversation/python/run-context";
import { PythonRun, RunOutput } from "@/components/conversation/python/run-output";
import type { TurnModel } from "@/components/conversation/types";
import type { ChatMessageDTO, FileAttachmentDTO } from "@/lib/hooks-chat";

const result = (over: Partial<PythonResult>): PythonResult => ({ ...readOutput({}), ...over });

const FILE: FileAttachmentDTO = {
  id: "f1",
  filename: "sales.csv",
  contentType: "text/csv",
  sizeBytes: 9,
  isImage: false,
  createdAt: "2026-11-27T00:00:00Z",
};

function scope(over: Partial<PythonScope> = {}): PythonScope {
  return {
    files: [],
    restrictedOf: (id) => (id === "m1" ? false : id === "m2" ? true : undefined),
    send: vi.fn(),
    ...over,
  };
}

function inScope(value: PythonScope, children: ReactNode) {
  return <PythonRunContext.Provider value={value}>{children}</PythonRunContext.Provider>;
}

const ANSWER = "Here:\n\n```python\nprint(6 * 7)\n```\n";

beforeEach(() => {
  previews.value = { data: { runScripts: true }, isError: false };
  runner.runPython.mockReset();
  download.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Run under a ```python block", () => {
  it("loads nothing before the first Run", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(inScope(scope(), <ChatMarkdown text={ANSWER} messageId="m1" artifactsOf="m1" />));
    expect(await screen.findByRole("button", { name: "Run" })).toBeTruthy();
    expect(runner.imported).toBe(false);
    expect(fetchSpy.mock.calls.filter(([url]) => String(url).includes("pyodide"))).toEqual([]);
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("is offered only inside a conversation, for a closed block of an answer", () => {
    const { container, rerender } = render(
      <ChatMarkdown text={ANSWER} messageId="m1" artifactsOf="m1" />,
    );
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
    // A user's own message has no `artifactsOf`.
    rerender(inScope(scope(), <ChatMarkdown text={ANSWER} memberAuthored />));
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
    // Still being written.
    rerender(
      inScope(scope(), <ChatMarkdown text={"```python\nprint(1)"} streaming artifactsOf="m1" />),
    );
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
    expect(container.textContent).toContain("print(1)");
  });

  it.each([
    [
      "a restricted answer",
      "m2",
      { data: { runScripts: true }, isError: false },
      /restricted content/,
    ],
    [
      "an unsaved answer",
      "run:r1",
      { data: { runScripts: true }, isError: false },
      /once the answer is saved/,
    ],
    ["the tenant switch off", "m1", { data: { runScripts: false }, isError: false }, /turned off/],
    ["an unreadable setting", "m1", { isError: true }, /could not be read/],
  ])("shows the reason instead of Run for %s", (_, id, settings, reason) => {
    previews.value = settings as typeof previews.value;
    render(inScope(scope(), <ChatMarkdown text={ANSWER} messageId={id} artifactsOf={id} />));
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
    expect(screen.getByRole("note").textContent).toMatch(reason);
  });

  it("runs the code and shows its output; sends the result as a capped user turn", async () => {
    runner.runPython.mockResolvedValue(result({ stdout: "x".repeat(9000), result: "42" }));
    const send = vi.fn();
    render(
      inScope(scope({ send }), <ChatMarkdown text={ANSWER} messageId="m1" artifactsOf="m1" />),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    await screen.findByTestId("python-output");
    expect(runner.runPython).toHaveBeenCalledWith(
      "print(6 * 7)",
      expect.objectContaining({ files: [], signal: expect.any(AbortSignal) }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Send result to chat" }));
    expect(send).toHaveBeenCalledTimes(1);
    const text = send.mock.calls[0][0] as string;
    expect(text.length).toBeLessThanOrEqual(LIMITS.message);
    expect(text).toMatch(/^Result of the Python code:\n\n```text\nx+/);
  });

  it("Stop aborts the run", async () => {
    let signal: AbortSignal | undefined;
    runner.runPython.mockImplementation(
      (_code: string, opts: { signal: AbortSignal; onStarted?: () => void }) => {
        signal = opts.signal;
        opts.onStarted?.();
        return new Promise(() => undefined);
      },
    );
    render(inScope(scope(), <PythonRun code="while True: pass" mode="scripts" />));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByText("Running…");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(signal?.aborted).toBe(true);
    expect(screen.getByText("Stopped.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run" })).toBeTruthy();
  });

  it("shows the first download's progress", async () => {
    runner.runPython.mockImplementation(
      (_code: string, opts: { onProgress?: (p: { loaded: number; total: number }) => void }) => {
        opts.onProgress?.({ loaded: 5 * 1024 * 1024, total: 10 * 1024 * 1024 });
        return new Promise(() => undefined);
      },
    );
    render(inScope(scope(), <PythonRun code="1" mode="scripts" />));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Loading Python… 5.0 of 10.0 MB")).toBeTruthy();
  });

  it("mounts the chat uploads picked, fetched by the page", async () => {
    const bytes = new TextEncoder().encode("a,b\n1,2\n");
    download.mockResolvedValue(new Blob([bytes]));
    runner.runPython.mockResolvedValue(result({ stdout: "ok\n" }));
    render(inScope(scope({ files: [FILE] }), <PythonRun code="1" mode="scripts" />));
    fireEvent.click(screen.getByRole("button", { name: "Files" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "sales.csv" }));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByTestId("python-output");
    expect(download).toHaveBeenCalledWith("f1");
    const { files } = runner.runPython.mock.calls[0][1] as {
      files: { name: string; bytes: ArrayBuffer }[];
    };
    expect(files.map((f) => [f.name, new TextDecoder().decode(f.bytes)])).toEqual([
      ["sales.csv", "a,b\n1,2\n"],
    ]);
  });

  it("shows a failure as text", async () => {
    runner.runPython.mockRejectedValue(new Error("<b>boom</b>"));
    render(inScope(scope(), <PythonRun code="1" mode="scripts" />));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("<b>boom</b>");
    expect(alert.querySelector("b")).toBeNull();
  });
});

describe("RunOutput", () => {
  it("renders everything as text; images only as PNG data URLs", () => {
    const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>';
    const { container } = render(
      <RunOutput
        result={result({
          stdout: evil,
          stderr: "Traceback: ValueError",
          result: evil,
          images: ["data:image/png;base64,iVBORw0KGgo="],
          tables: [{ columns: ["<b>n</b>"], rows: [[evil]] }],
        })}
      />,
    );
    expect(container.querySelectorAll("script, b")).toHaveLength(0);
    const images = [...container.querySelectorAll("img")];
    expect(images.map((i) => i.getAttribute("src"))).toEqual([
      "data:image/png;base64,iVBORw0KGgo=",
    ]);
    expect(screen.getByTestId("python-stderr").className).toContain("text-destructive");
    const table = within(container.querySelector("table")!);
    expect(table.getByText("<b>n</b>")).toBeTruthy();
    expect(table.getByText(evil)).toBeTruthy();
  });

  it("says when the wall clock stopped the code", () => {
    render(<RunOutput result={result({ timedOut: true })} />);
    expect(screen.getByRole("status").textContent).toBe("Stopped after 30 seconds.");
  });
});

describe("usePythonScopeFor", () => {
  function Probe({ turns, onScope }: { turns: TurnModel[]; onScope: (s: PythonScope) => void }) {
    onScope(usePythonScopeFor(turns, null));
    return null;
  }

  it("offers the uploads on the path up to 20 MB and knows restricted answers", () => {
    const big = { ...FILE, id: "f2", filename: "big.bin", sizeBytes: LIMITS.fileBytes + 1 };
    const user = (id: string, attachments: FileAttachmentDTO[]): TurnModel => ({
      kind: "user",
      key: id,
      message: { id, attachments } as unknown as ChatMessageDTO,
    });
    const turns: TurnModel[] = [
      user("u1", [FILE, big]),
      {
        kind: "assistant",
        key: "a1",
        replyToId: "u1",
        variants: [{ id: "a1", restricted: true } as unknown as ChatMessageDTO],
        liveRunId: null,
      },
      user("u2", [FILE]),
    ];
    let seen: PythonScope | null = null;
    act(() => {
      render(<Probe turns={turns} onScope={(s) => (seen = s)} />);
    });
    const s = seen as unknown as PythonScope;
    expect(s.files.map((f) => f.id)).toEqual(["f1"]);
    expect(s.restrictedOf("a1")).toBe(true);
    expect(s.restrictedOf("nope")).toBeUndefined();
    expect(s.send).toBeNull();
  });
});

// The panel offers Run on a python artifact's Code tab with the panel's mode.
describe("the artifact panel", () => {
  it("offers Run for a python artifact and the reason when static", async () => {
    const { ArtifactPanel } = await import("@/components/conversation/artifacts/artifact-panel");
    const source = Array.from({ length: 16 }, (_, i) => `print(${i})`).join("\n");
    const artifact = {
      id: "m1:0",
      messageId: "m1",
      kind: "code",
      language: "python",
      title: "python",
      source,
      closed: true,
    } as const;
    const { rerender } = render(
      inScope(
        scope(),
        <ArtifactPanel
          artifact={artifact as never}
          versions={[artifact as never]}
          mode="scripts"
          onSelect={() => undefined}
          onClose={() => undefined}
        />,
      ),
    );
    expect(await screen.findByRole("button", { name: "Run" })).toBeTruthy();
    rerender(
      inScope(
        scope(),
        <ArtifactPanel
          artifact={artifact as never}
          versions={[artifact as never]}
          mode="restricted"
          onSelect={() => undefined}
          onClose={() => undefined}
        />,
      ),
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: "Run" })).toBeNull());
    expect(screen.getByText(/restricted content/)).toBeTruthy();
  });
});
