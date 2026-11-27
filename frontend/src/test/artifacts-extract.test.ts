// Which fenced blocks of an answer open beside the chat (spec §5.2): ```html,
// ```svg, ```mermaid, and any other block of 15 lines or more. Derived from
// the message content at render time, so old chats get them too.
import { describe, expect, it } from "vitest";

import {
  artifactsOnPath,
  extractArtifacts,
  fileNameFor,
  versionsOf,
} from "@/components/conversation/artifacts/extract";

const lines = (n: number, prefix = "x = ") => Array.from({ length: n }, (_, i) => `${prefix}${i}`);

describe("extractArtifacts", () => {
  it("takes html, svg and mermaid blocks of any length", () => {
    const content = [
      "Here:",
      "```html",
      "<p>hi</p>",
      "```",
      "and",
      "```svg",
      '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
      "```",
      "```mermaid",
      "graph TD; A-->B",
      "```",
    ].join("\n");
    const found = extractArtifacts(content, "m1");
    expect(found.map((a) => [a.id, a.kind, a.language, a.closed])).toEqual([
      ["m1:0", "html", "html", true],
      ["m1:1", "svg", "svg", true],
      ["m1:2", "mermaid", "mermaid", true],
    ]);
    expect(found[0].source).toBe("<p>hi</p>");
    expect(found.every((a) => a.messageId === "m1")).toBe(true);
  });

  it("takes other code only from 15 lines on", () => {
    const short = ["```python", ...lines(14), "```"].join("\n");
    const long = ["```python", ...lines(15), "```"].join("\n");
    expect(extractArtifacts(short, "m")).toEqual([]);
    const [artifact] = extractArtifacts(long, "m");
    expect(artifact.kind).toBe("code");
    expect(artifact.language).toBe("python");
    expect(artifact.source.split("\n")).toHaveLength(15);
  });

  it("never takes KaTeX's math blocks or inline code", () => {
    const content = ["```math", ...lines(20, "a_"), "```", "and `<b>html</b>` inline"].join("\n");
    expect(extractArtifacts(content, "m")).toEqual([]);
  });

  it("numbers artifacts by their place among the message's artifacts", () => {
    const content = [
      "```js",
      "short()",
      "```",
      "```html",
      "<p>a</p>",
      "```",
      "```html",
      "<p>b</p>",
      "```",
    ].join("\n");
    expect(extractArtifacts(content, "m").map((a) => a.id)).toEqual(["m:0", "m:1"]);
  });

  it("marks a block still being written as not closed", () => {
    const content = "Building it:\n\n```html\n<!doctype html>\n<title>Clock</title>\n<p>12:";
    const [artifact] = extractArtifacts(content, "m");
    expect(artifact.closed).toBe(false);
    expect(artifact.title).toBe("Clock");
    expect(artifact.source).toBe("<!doctype html>\n<title>Clock</title>\n<p>12:");
  });

  it("closes only on a fence at least as long, of the same character", () => {
    const content = ["````html", "```", "<p>x</p>", "~~~", "````"].join("\n");
    const [artifact] = extractArtifacts(content, "m");
    expect(artifact.closed).toBe(true);
    expect(artifact.source).toBe("```\n<p>x</p>\n~~~");
  });

  it("reads a fence inside a list item without its indentation", () => {
    const content = "1. Page:\n   ```html\n   <p>x</p>\n   ```\n";
    const [artifact] = extractArtifacts(content, "m");
    expect(artifact.source).toBe("<p>x</p>");
    expect(artifact.closed).toBe(true);
  });

  it("reads a fence only when indented by three spaces at most", () => {
    expect(extractArtifacts("    ```html\n    <p>x</p>\n    ```", "m")).toEqual([]);
    expect(extractArtifacts("\t```html\n<p>x</p>\n```", "m")).toEqual([]);
    // A closing fence indented by four spaces is content, not the end.
    const [open] = extractArtifacts("```html\n<p>x</p>\n    ```", "m");
    expect(open.closed).toBe(false);
  });

  it("reads CRLF line ends like LF", () => {
    const [artifact] = extractArtifacts("```html\r\n<p>x</p>\r\n```\r\n", "m");
    expect(artifact.source).toBe("<p>x</p>");
  });
});

describe("artifact titles", () => {
  const titleOf = (language: string, body: string[]) =>
    extractArtifacts(["```" + language, ...body, "```"].join("\n"), "m")[0].title;

  it("prefers the document's <title>", () => {
    expect(titleOf("html", ["<h1>Big</h1>", "<title> Sales &amp; Costs </title>"])).toBe(
      "Sales & Costs",
    );
    expect(titleOf("svg", ["<svg><title>Logo</title></svg>"])).toBe("Logo");
  });

  it("then the first heading", () => {
    expect(titleOf("html", ["<body>", "<h1 class='x'>Team <b>board</b></h1>"])).toBe("Team board");
    expect(titleOf("markdown", ["# Release notes", ...lines(15)])).toBe("Release notes");
  });

  it("then a comment at the top", () => {
    expect(titleOf("ts", ["// Invoice parser", ...lines(15)])).toBe("Invoice parser");
    expect(titleOf("sql", ["-- Monthly revenue", ...lines(15)])).toBe("Monthly revenue");
    expect(titleOf("mermaid", ["%% Order flow", "graph TD; A-->B"])).toBe("Order flow");
    expect(titleOf("css", ["/* Theme */", ...lines(15)])).toBe("Theme");
    expect(titleOf("html", ["<!-- Landing page -->", "<p>x</p>"])).toBe("Landing page");
  });

  it("else the language, and never a shebang", () => {
    expect(titleOf("python", ["#!/usr/bin/env python", ...lines(15)])).toBe("python");
    expect(titleOf("", lines(15))).toBe("code");
  });

  it("caps a long title", () => {
    const title = titleOf("html", [`<title>${"a".repeat(200)}</title>`]);
    expect(title.length).toBeLessThanOrEqual(80);
  });
});

describe("versions and files", () => {
  const page = (title: string, body: string) =>
    "```html\n<title>" + title + "</title>\n" + body + "\n```";

  it("versions are the artifacts with the same title on the path, in order", () => {
    const all = artifactsOnPath([
      { id: "a", content: page("Clock", "v1") },
      { id: "b", content: "no artifact" },
      { id: "c", content: page("Other", "x") + "\n" + page("Clock", "v2") },
    ]);
    const versions = versionsOf(all[0], all);
    expect(versions.map((a) => a.id)).toEqual(["a:0", "c:1"]);
    expect(versions.map((a) => a.source.endsWith("v2"))).toEqual([false, true]);
  });

  it("an artifact not on the path is its own only version", () => {
    const [live] = extractArtifacts(page("Clock", "live"), "live");
    expect(versionsOf(live, [])).toEqual([live]);
  });

  it("names the download after the title, with the kind's extension", () => {
    const [html] = extractArtifacts(page("Sales Übersicht 2026!", "x"), "m");
    expect(fileNameFor(html)).toBe("sales-ubersicht-2026.html");
    const [py] = extractArtifacts(["```python", ...lines(15), "```"].join("\n"), "m");
    expect(fileNameFor(py)).toBe("python.py");
    const [svg] = extractArtifacts("```svg\n<svg></svg>\n```", "m");
    expect(fileNameFor(svg)).toBe("svg.svg");
    const [mmd] = extractArtifacts("```mermaid\ngraph TD; A-->B\n```", "m");
    expect(fileNameFor(mmd)).toBe("mermaid.mmd");
    const [other] = extractArtifacts(["```", ...lines(15), "```"].join("\n"), "m");
    expect(fileNameFor(other)).toBe("code.txt");
  });
});
