import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentAvatar, hueFromOklch } from "@/components/agent-avatar";

describe("AgentAvatar", () => {
  it("renders the same picture for the same seed", () => {
    const a = render(<AgentAvatar seed="agent-1" size={36} title="Nova" />);
    const b = render(<AgentAvatar seed="agent-1" size={36} title="Nova" />);
    expect(a.container.querySelector("img")?.src).toEqual(b.container.querySelector("img")?.src);
  });

  it("renders a different picture for a different seed", () => {
    const a = render(<AgentAvatar seed="agent-1" size={36} title="Nova" />);
    const b = render(<AgentAvatar seed="agent-2" size={36} title="Atlas" />);
    expect(a.container.querySelector("img")?.src).not.toEqual(
      b.container.querySelector("img")?.src,
    );
  });

  it("uses the title as the accessible name by default", () => {
    const { container } = render(<AgentAvatar seed="agent-1" size={36} title="Nova" />);
    expect(container.querySelector("img")?.alt).toBe("Nova");
  });

  it("hides itself from assistive tech and drops the accessible name when ariaHidden", () => {
    const { container } = render(<AgentAvatar seed="agent-1" size={36} title="Nova" ariaHidden />);
    const img = container.querySelector("img");
    expect(img?.getAttribute("aria-hidden")).toBe("true");
    expect(img?.alt).toBe("");
  });
});

describe("hueFromOklch", () => {
  it("extracts the numeric hue from an oklch() string", () => {
    expect(hueFromOklch("oklch(0.75 0.15 30)")).toBe(30);
    expect(hueFromOklch("oklch(0.70 0.14 155)")).toBe(155);
  });

  it("returns undefined for a value that isn't an oklch() string", () => {
    expect(hueFromOklch("#000")).toBeUndefined();
  });
});
