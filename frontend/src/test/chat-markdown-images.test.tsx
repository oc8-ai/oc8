// Ruling 12: an answer's image never loads from its host until the member asks.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChatMarkdown } from "@/components/chat-markdown";

describe("ChatMarkdown images", () => {
  it("shows a load button and fetches nothing until it is clicked", () => {
    const { container } = render(<ChatMarkdown text="![Chart](https://img.test/c.png)" />);
    expect(container.querySelector('img[src^="https"]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Load image from img\.test/ }));
    const img = container.querySelector('img[src^="https"]');
    expect(img).toHaveAttribute("src", "https://img.test/c.png");
    expect(img).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(img).toHaveAttribute("alt", "Chart");
  });

  it("never offers a non-https image", () => {
    const { container } = render(<ChatMarkdown text="![x](http://img.test/c.png)" />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByRole("button", { name: /Load image/ })).toBeNull();
  });
});
