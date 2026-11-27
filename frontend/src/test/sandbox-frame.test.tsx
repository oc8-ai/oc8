// The preview frame for untrusted HTML (spec §4.1, §9). jsdom runs no
// sandbox, so these assert what the browser enforces from: the frame's
// sandbox flags, the CSP as the srcdoc's first head child, and which
// messages reach the parent. The runtime checks (a fetch to /api fails,
// parent.document throws, ...) are part of the live verification; the page
// for them is SANDBOX_PROBE below.
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SandboxFrame } from "@/components/conversation/artifacts/sandbox-frame";
import {
  buildSandboxSrcdoc,
  SANDBOX_CSP,
  sandboxCsp,
} from "@/components/conversation/artifacts/sandbox-srcdoc";

/** For the live check: open as an artifact, each line must read "blocked". */
const SANDBOX_PROBE = `<title>Sandbox probe</title><pre id="out"></pre><script>
const out = document.getElementById("out");
const line = (name, ok) => (out.textContent += name + ": " + (ok ? "blocked" : "OPEN") + "\\n");
const tries = (name, fn) => { try { fn(); line(name, false); } catch { line(name, true); } };
tries("parent.document", () => parent.document.title);
tries("top.location.href", () => top.location.href);
tries("localStorage", () => localStorage.getItem("x"));
tries("document.cookie", () => document.cookie);
line("window.open", window.open("https://example.com") === null);
fetch("/api/v1/auth/me").then(() => line("fetch /api", false), () => line("fetch /api", true));
</script>`;

function head(srcdoc: string): HTMLHeadElement {
  return new DOMParser().parseFromString(srcdoc, "text/html").head;
}

describe("buildSandboxSrcdoc", () => {
  it("puts the CSP meta first in <head>, before anything of the page", () => {
    const page =
      '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src *">' +
      "<title>T</title></head><body><p>x</p></body></html>";
    const doc = new DOMParser().parseFromString(buildSandboxSrcdoc(page), "text/html");
    const first = doc.head.firstElementChild;
    expect(first?.tagName).toBe("META");
    expect(first?.getAttribute("http-equiv")).toBe("Content-Security-Policy");
    expect(first?.getAttribute("content")).toBe(SANDBOX_CSP);
    // The page's own CSP meta lands outside <head>, where it is not read.
    expect(doc.head.querySelectorAll('meta[http-equiv="Content-Security-Policy"]')).toHaveLength(1);
    expect(doc.body.querySelector("p")?.textContent).toBe("x");
  });

  it("gives a page no network, frames or forms", () => {
    expect(SANDBOX_CSP).toContain("default-src 'none'");
    expect(SANDBOX_CSP).toContain("script-src 'unsafe-inline'");
    expect(SANDBOX_CSP).toContain("img-src data: blob:");
    expect(SANDBOX_CSP).not.toMatch(/connect-src|frame-src|child-src|'self'|https?:/);
    expect(SANDBOX_CSP).toContain("form-action 'none'");
  });

  it("lets extraCsp replace a directive but never open the network", () => {
    const csp = sandboxCsp(
      "script-src 'unsafe-inline' 'wasm-unsafe-eval' blob:; worker-src blob:; connect-src *; form-action *",
    );
    expect(csp).toContain("script-src 'unsafe-inline' 'wasm-unsafe-eval' blob:");
    expect(csp.match(/script-src/g)).toHaveLength(1);
    expect(csp).toContain("worker-src blob:");
    expect(csp).not.toContain("connect-src");
    expect(csp).toContain("form-action 'none'");
    expect(
      head(buildSandboxSrcdoc("", "worker-src blob:")).firstElementChild?.getAttribute("content"),
    ).toContain("worker-src blob:");
  });

  it("escapes the policy into its attribute", () => {
    const meta = head(buildSandboxSrcdoc("", 'img-src "x"><script>')).firstElementChild;
    expect(meta?.getAttribute("content")).toContain('img-src "x"><script>');
    expect(head(buildSandboxSrcdoc("", 'img-src "x"><script>')).querySelector("script")).toBeNull();
  });
});

describe("SandboxFrame", () => {
  it("is sandboxed with allow-scripts only", () => {
    render(<SandboxFrame html={SANDBOX_PROBE} />);
    const frame = screen.getByTitle("Preview") as HTMLIFrameElement;
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(frame.getAttribute("src")).toBeNull();
    expect(frame.getAttribute("srcdoc")).toBe(buildSandboxSrcdoc(SANDBOX_PROBE));
    expect(head(frame.getAttribute("srcdoc")!).firstElementChild?.getAttribute("http-equiv")).toBe(
      "Content-Security-Policy",
    );
  });

  it("passes on messages from its own frame only", () => {
    const onMessage = vi.fn();
    render(<SandboxFrame html="<p>x</p>" onMessage={onMessage} />);
    const frame = screen.getByTitle("Preview") as HTMLIFrameElement;
    act(() => {
      window.dispatchEvent(new MessageEvent("message", { data: "from elsewhere", source: window }));
      window.dispatchEvent(new MessageEvent("message", { data: "no source" }));
      window.dispatchEvent(
        new MessageEvent("message", { data: { ok: 1 }, source: frame.contentWindow }),
      );
    });
    expect(onMessage).toHaveBeenCalledTimes(1);
    expect(onMessage).toHaveBeenCalledWith({ ok: 1 });
  });

  it("takes the frame away when it navigates off its document", () => {
    const onMessage = vi.fn();
    render(<SandboxFrame html="<a href='https://example.com'>x</a>" onMessage={onMessage} />);
    const frame = screen.getByTitle("Preview");
    fireEvent.load(frame);
    expect(screen.getByTitle("Preview")).toBeInTheDocument();
    fireEvent.load(frame);
    expect(screen.queryByTitle("Preview")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("tried to open another page");
    fireEvent.click(screen.getByRole("button", { name: "Show the preview again" }));
    expect(screen.getByTitle("Preview")).toBeInTheDocument();
  });

  it("a new document is no navigation", () => {
    const { rerender } = render(<SandboxFrame html="<p>1</p>" />);
    fireEvent.load(screen.getByTitle("Preview"));
    rerender(<SandboxFrame html="<p>2</p>" />);
    fireEvent.load(screen.getByTitle("Preview"));
    expect(screen.getByTitle("Preview")).toBeInTheDocument();
  });
});
