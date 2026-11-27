// The preview frame for untrusted HTML (spec §4.1, §9). jsdom runs no
// sandbox, so these assert what the browser enforces from: the frame's
// sandbox flags, the CSP as the srcdoc's first head child, the guard script
// before the page, the stripped markup, and which messages reach the
// parent. The same in a real browser, with a counted outside server:
// e2e/artifact-sandbox.spec.ts (`npm run test:e2e`).
import { act, fireEvent, render, screen } from "@testing-library/react";
import { JSDOM } from "jsdom";
import { describe, expect, it, vi } from "vitest";

import { SandboxFrame } from "@/components/conversation/artifacts/sandbox-frame";
import {
  buildSandbox,
  buildSandboxSrcdoc,
  GUARD,
  sanitizePage,
  SANDBOX_CSP,
  sandboxCsp,
  withoutNetworkHints,
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
    // The guard is the first script, in our head, before any of the page.
    const scripts = doc.querySelectorAll("script");
    expect(scripts[0].parentElement).toBe(doc.head);
    expect(scripts[0].textContent).toBe(GUARD);
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
    expect(SANDBOX_CSP).toContain("webrtc 'block'");
    const hints = head(buildSandboxSrcdoc("")).querySelector(
      'meta[http-equiv="x-dns-prefetch-control"]',
    );
    expect(hints?.getAttribute("content")).toBe("off");
  });

  it("lets extraCsp replace a directive but never open the network", () => {
    const csp = sandboxCsp(
      "script-src 'unsafe-inline' 'wasm-unsafe-eval' blob:; worker-src blob:; connect-src *; form-action *; webrtc 'allow'; frame-src *",
    );
    expect(csp).toContain("script-src 'unsafe-inline' 'wasm-unsafe-eval' blob:");
    expect(csp.match(/script-src/g)).toHaveLength(1);
    expect(csp).toContain("worker-src blob:");
    expect(csp).not.toContain("connect-src");
    expect(csp).toContain("form-action 'none'");
    expect(csp).toContain("webrtc 'block'");
    expect(csp).not.toContain("frame-src");
    expect(
      head(buildSandboxSrcdoc("", "worker-src blob:")).firstElementChild?.getAttribute("content"),
    ).toContain("worker-src blob:");
  });

  it("puts GUARD as the first script before any artifact markup; a static page has none", () => {
    const page = "<p id='first'>x</p><script>1</script>";
    const scripted = buildSandboxSrcdoc(page);
    expect(scripted.indexOf(`<script>${GUARD}</script>`)).toBeGreaterThan(0);
    expect(scripted.indexOf("<script>")).toBe(scripted.indexOf(`<script>${GUARD}</script>`));
    expect(scripted.indexOf(GUARD)).toBeLessThan(scripted.indexOf("<p id"));
    const fixed = buildSandboxSrcdoc(page, "script-src 'unsafe-inline' blob:", { scripts: false });
    const doc = new DOMParser().parseFromString(fixed, "text/html");
    expect(fixed).not.toContain(GUARD);
    expect(doc.querySelectorAll("script")).toHaveLength(1); // the page's own, inert
    const csp = doc.head.firstElementChild?.getAttribute("content") ?? "";
    expect(csp).toContain("script-src 'none'");
    expect(csp.match(/script-src/g)).toHaveLength(1);
  });

  it("escapes the policy into its attribute", () => {
    const meta = head(buildSandboxSrcdoc("", 'img-src "x"><script>')).firstElementChild;
    expect(meta?.getAttribute("content")).toContain('img-src "x"><script>');
    expect(
      head(buildSandboxSrcdoc("", 'img-src "x"><script>')).querySelectorAll("script"),
    ).toHaveLength(1);
  });
});

describe("withoutNetworkHints", () => {
  const OUT = "https://out.example";
  it("drops refresh, base and resource hints, however they are written", () => {
    const page =
      `<META content="0;url=${OUT}" HTTP-EQUIV=" Refresh "><base href="${OUT}/">` +
      `<link rel="DNS-Prefetch" href="//out.example"><link href="${OUT}" rel="icon preconnect">` +
      `<link rel=prefetch href="${OUT}/p"><link rel="prerender" href="${OUT}/r">` +
      `<link rel="preload" as="image" href="${OUT}/i"><link rel="modulepreload" href="${OUT}/m.js">` +
      `<meta charset="utf-8"><link rel="stylesheet" href="data:text/css,p{}"><p>kept</p>`;
    const out = withoutNetworkHints(page);
    expect(out).not.toContain(OUT);
    expect(out).not.toContain("out.example");
    expect(out).not.toMatch(/<base|refresh/i);
    expect(out).toContain('<meta charset="utf-8">');
    expect(out).toContain('rel="stylesheet"');
    expect(out).toContain("<p>kept</p>");
  });

  it("turns declarative shadow roots into plain templates", () => {
    const out = withoutNetworkHints(
      '<div><template shadowrootmode="open"><iframe srcdoc="x"></iframe></template></div>',
    );
    expect(out).not.toMatch(/shadowroot/i);
  });

  it("keeps the page's scripts, styles and body attributes", () => {
    const page =
      '<!doctype html><html lang="de"><head><title>T</title><style>p{color:red}</style></head>' +
      '<body onload="init()"><script>function init(){document.title = "a<b"}</script></body></html>';
    const doc = new DOMParser().parseFromString(buildSandboxSrcdoc(page), "text/html");
    expect(doc.body.getAttribute("onload")).toBe("init()");
    expect(doc.documentElement.lang).toBe("de");
    expect(doc.querySelector("style")?.textContent).toBe("p{color:red}");
    expect(doc.querySelectorAll("script")[1].textContent).toContain('"a<b"');
  });

  it("drops frames, noscript and comments (where the two parsers differ)", () => {
    const out = withoutNetworkHints(
      '<iframe srcdoc="x"></iframe><frameset></frameset><object data="x"></object><embed src="x">' +
        "<portal src=x></portal><fencedframe></fencedframe>" +
        '<noscript><p title="</noscript><iframe srcdoc=y></iframe>"></p></noscript>' +
        "<!-- <iframe srcdoc=z></iframe> --><p>kept</p>",
    );
    expect(out).not.toMatch(/iframe|frameset|object|embed|portal|fencedframe|noscript|<!--/i);
    expect(out).toContain("<p>kept</p>");
  });

  // Mutation XSS: markup whose serialisation parses back into something
  // else. What the frame's parser reads from the output must hold no frame.
  it.each([
    '<math><mtext><table><mglyph><style><!--</style><img title="--><iframe srcdoc=x>">',
    '<svg></p><style><a id="</style><iframe srcdoc=x>">',
    "<form><math><mtext></form><form><mglyph><style></math><iframe srcdoc=x>",
    '<svg><foreignObject><p><style><a title="</style><iframe srcdoc=x>"></a></style></p></foreignObject></svg>',
    '<math><annotation-xml encoding="text/html"><style><img title="</style><iframe srcdoc=x>"></annotation-xml></math>',
    "<noscript><style></noscript><iframe srcdoc=x></style>",
  ])("leaves no frame after the frame parses it again: %s", (payload) => {
    const { html } = sanitizePage(payload);
    for (let i = 0; i < 3; i += 1) {
      const doc = new DOMParser().parseFromString(html, "text/html");
      expect(doc.querySelectorAll("iframe, foreignObject, mglyph, annotation-xml")).toHaveLength(0);
    }
  });

  it("keeps an ordinary inline SVG chart as it is", () => {
    const chart =
      '<svg viewBox="0 0 100 50" role="img"><title>Sales</title><g fill="steelblue">' +
      '<rect x="0" y="10" width="20" height="40"></rect><rect x="30" y="20" width="20" height="30"></rect></g>' +
      '<path d="M0 50L100 0" stroke="black"></path><text x="5" y="48">Q1</text>' +
      '<circle cx="80" cy="10" r="4"></circle></svg>';
    const { html, stable } = sanitizePage(`<h1>Report</h1>${chart}`);
    expect(stable).toBe(true);
    const svg = new DOMParser().parseFromString(html, "text/html").querySelector("svg");
    expect(svg?.querySelectorAll("rect")).toHaveLength(2);
    expect(svg?.querySelector("path")?.getAttribute("d")).toBe("M0 50L100 0");
    expect(svg?.querySelector("text")?.textContent).toBe("Q1");
    expect(svg?.querySelector("circle")).not.toBeNull();
  });

  it("shows a page it cannot settle without scripts", () => {
    const Real = DOMParser;
    // A parser whose every round trip changes the page.
    class Growing {
      parseFromString(html: string, type: DOMParserSupportedType) {
        return new Real().parseFromString(`${html}<i></i>`, type);
      }
    }
    vi.stubGlobal("DOMParser", Growing);
    try {
      const built = buildSandbox("<p>x</p><script>1</script>");
      expect(built).toMatchObject({ scripts: false, unstable: true });
      expect(built.srcdoc).not.toContain(GUARD);
      expect(built.srcdoc).toContain("script-src 'none'");
      render(<SandboxFrame html="<p>x</p>" />);
      expect(screen.getByTitle("Preview").getAttribute("sandbox")).toBe("");
      expect(screen.getByRole("note")).toHaveTextContent("could not be read safely");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("is stripped in the srcdoc too", () => {
    const srcdoc = buildSandboxSrcdoc(`<meta http-equiv="refresh" content="0;url=${OUT}">`);
    expect(srcdoc).not.toContain(OUT);
  });
});

describe("GUARD, run", () => {
  function frame(page: string) {
    const dom = new JSDOM(buildSandboxSrcdoc(page), { runScripts: "dangerously" });
    return dom.window as unknown as Window & Record<string, unknown>;
  }

  it("takes the network APIs away for good, before the page runs", () => {
    const win = frame(
      "<script>window.seen = typeof fetch + ' ' + typeof XMLHttpRequest + ' ' + typeof WebSocket + ' ' + typeof navigator.sendBeacon;" +
        "try { delete window.fetch; Object.defineProperty(window, 'fetch', { value: 1 }); } catch (e) {}" +
        "try { window.XMLHttpRequest = function () {}; } catch (e) {}</script>",
    );
    expect(win.seen).toBe("undefined undefined undefined undefined");
    expect(win.fetch).toBeUndefined();
    expect(win.XMLHttpRequest).toBeUndefined();
    expect(typeof (win.Element as unknown as typeof Element).prototype.attachShadow).toBe(
      "undefined",
    );
  });

  it("keeps a nested frame out, synchronously, whatever the page polluted", () => {
    const win = frame(
      "<script>window.out = [];" +
        "Array.prototype[Symbol.iterator] = function* () {};" +
        "Element.prototype.remove = () => {}; Element.prototype.matches = () => false;" +
        "Node.prototype.removeChild = (n) => n;" +
        "const t = (f) => { try { f(); out.push('ok'); } catch (e) { out.push('blocked'); } };" +
        "t(() => document.createElement('iframe'));" +
        "const d = new DOMParser().parseFromString('<div><iframe></iframe></div><object></object>', 'text/html');" +
        "t(() => document.body.append(document.adoptNode(d.body.firstChild)));" +
        "t(() => document.body.appendChild(document.adoptNode(d.querySelector('object'))));" +
        "const x = document.createElement('div'); document.body.append(x);" +
        "t(() => { x.innerHTML = '<embed src=x><iframe></iframe>'; });" +
        "window.left = document.querySelectorAll('iframe,frame,object,embed').length;</script>",
    );
    expect(win.out).toEqual(["blocked", "ok", "blocked", "blocked"]);
    // Counted in the same task, before any nested document could load.
    expect(win.left).toBe(0);
  });

  it("refuses a resource hint or refresh before the browser sees it, by any attribute path", () => {
    const win = frame(
      '<link id="l" rel="icon" href="data:,x"><meta id="n" name="x" content="y"><script>' +
        "const l = document.getElementById('l'), n = document.getElementById('n');" +
        "const bad = { n: 0, toString() { this.n += 1; return this.n === 1 ? 'icon' : 'preconnect'; } };" +
        "l.setAttribute('rel', 'preconnect'); l.setAttribute('rel', bad); l.rel = 'dns-prefetch';" +
        "l.relList.add('preconnect'); l.relList.replace('icon', 'prefetch'); l.relList.value = 'preload';" +
        "l.getAttributeNode('rel').value = 'preconnect'; l.getAttributeNode('rel').nodeValue = 'preconnect';" +
        "l.getAttributeNode('rel').textContent = 'preconnect';" +
        "const a = document.createAttribute('rel'); a.value = 'prerender'; l.setAttributeNode(a);" +
        "const b = document.createAttribute('http-equiv'); b.value = 'refresh'; n.attributes.setNamedItem(b);" +
        "n.httpEquiv = 'refresh'; n.setAttribute('http-equiv', 'Refresh');" +
        "document.body.classList.add('preconnect');" +
        "const m = document.createElement('meta'); document.head.append(m); m.httpEquiv = 'refresh';" +
        "const k = document.createElement('div'); try { k.innerHTML = '<link rel=dns-prefetch href=//x>'; } catch (e) {} document.body.append(k);" +
        "window.rel = l.getAttribute('rel'); window.equiv = n.getAttribute('http-equiv'); window.cls = document.body.className;" +
        "window.left = document.querySelectorAll('link[rel*=pre], meta[http-equiv*=refresh i], link[rel*=prefetch]').length;</script>",
    );
    // Every change was refused on the spot: the elements are as they were.
    expect(win.rel).toBe("icon");
    expect(win.equiv).toBeNull();
    // classList shares DOMTokenList with relList and is left alone.
    expect(win.cls).toBe("preconnect");
    expect(win.left).toBe(0);
  });

  it("leaves no prefetch switch to rewrite into a refresh", () => {
    const win = frame(
      "<script>window.found = document.querySelectorAll('meta[http-equiv]').length;</script>",
    );
    // The CSP meta only.
    expect(win.found).toBe(1);
  });

  it("removes document.write and the other ways to parse a shadow root", () => {
    const win = frame("");
    const doc = win.document as unknown as Record<string, unknown>;
    expect(doc.write).toBeUndefined();
    expect(doc.writeln).toBeUndefined();
    expect(doc.open).toBeUndefined();
    expect(doc.execCommand).toBeUndefined();
  });
});

describe("SandboxFrame", () => {
  it("is a static frame without scripts when asked", () => {
    render(<SandboxFrame html="<p>x</p>" scripts={false} />);
    const frame = screen.getByTitle("Preview");
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("srcdoc")).not.toContain(GUARD);
    expect(frame.getAttribute("srcdoc")).toContain("script-src 'none'");
  });

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
