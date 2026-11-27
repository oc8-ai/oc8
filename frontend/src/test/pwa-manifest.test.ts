import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Route, ThemeColorMeta } from "../routes/__root";

const PUBLIC = resolve(process.cwd(), "public");

interface ManifestIcon {
  src: string;
  sizes: string;
  type: string;
  purpose?: string;
}

const manifest = JSON.parse(readFileSync(resolve(PUBLIC, "manifest.webmanifest"), "utf8")) as {
  name: string;
  short_name: string;
  start_url: string;
  scope: string;
  display: string;
  icons: ManifestIcon[];
};

/** Width and height from a PNG's IHDR chunk. */
function pngSize(path: string): string {
  const bytes = readFileSync(resolve(PUBLIC, path.replace(/^\//, "")));
  expect(bytes.subarray(1, 4).toString("ascii")).toBe("PNG");
  return `${bytes.readUInt32BE(16)}x${bytes.readUInt32BE(20)}`;
}

describe("web app manifest", () => {
  it("names the app and opens it standalone at the root", () => {
    expect(manifest.name).toMatch(/oc8/);
    expect(manifest.short_name).toBe("oc8");
    expect(manifest.short_name.length).toBeLessThanOrEqual(12);
    expect(manifest.display).toBe("standalone");
    expect(manifest.start_url).toBe("/");
    expect(manifest.scope).toBe("/");
  });

  it("ships 192 and 512 icons, plain and maskable, whose files match their sizes", () => {
    for (const size of ["192x192", "512x512"]) {
      const plain = manifest.icons.find((i) => i.sizes === size && (i.purpose ?? "any") === "any");
      const maskable = manifest.icons.find((i) => i.sizes === size && i.purpose === "maskable");
      expect(plain, `any ${size}`).toBeDefined();
      expect(maskable, `maskable ${size}`).toBeDefined();
    }
    for (const icon of manifest.icons) {
      expect(icon.type).toBe("image/png");
      expect(pngSize(icon.src)).toBe(icon.sizes);
    }
  });
});

describe("document head", () => {
  const head = Route.options.head!({} as never) as {
    meta: Record<string, string>[];
    links: Record<string, string>[];
  };
  const meta = (name: string) => head.meta.filter((m) => m.name === name);

  it("links the manifest", () => {
    expect(head.links).toContainEqual({ rel: "manifest", href: "/manifest.webmanifest" });
  });

  it("lets the layout reach under the notch and home indicator", () => {
    expect(meta("viewport")[0].content).toContain("viewport-fit=cover");
    expect(meta("viewport")[0].content).toContain("interactive-widget=resizes-content");
  });

  it("colours the browser chrome for light and dark", () => {
    // Not through head(): HeadContent keeps one meta per name.
    expect(meta("theme-color")).toEqual([]);
    const html = renderToStaticMarkup(createElement(ThemeColorMeta));
    const tags = [
      ...html.matchAll(/<meta name="theme-color" media="([^"]+)" content="([^"]+)"\/>/g),
    ];
    expect(tags.map((m) => m[1])).toEqual([
      "(prefers-color-scheme: light)",
      "(prefers-color-scheme: dark)",
    ]);
    expect(tags.every((m) => /^#[0-9a-f]{6}$/i.test(m[2]))).toBe(true);
  });

  it("opens full screen when added to an iPhone home screen", () => {
    expect(meta("apple-mobile-web-app-capable")[0]?.content).toBe("yes");
    expect(meta("mobile-web-app-capable")[0]?.content).toBe("yes");
    expect(meta("apple-mobile-web-app-title")[0]?.content).toBe("oc8");
  });
});
