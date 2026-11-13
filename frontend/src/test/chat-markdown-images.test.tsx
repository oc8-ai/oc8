// Ruling 12: an answer's image never loads from its host until the member asks.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// ChatMarkdown's record-link check, without a server: in a saved answer no
// link points at a connected system, so each is an ordinary one.
vi.mock("@/lib/record-links", async (orig) => ({
  ...(await orig<typeof import("@/lib/record-links")>()),
  useVerifiedLinks: (messageId: string | undefined, urls: string[]) => ({
    data: messageId
      ? new Map(
          urls.map((url) => [
            url,
            { url, host: new URL(url).hostname, connected: false, issued: false },
          ]),
        )
      : undefined,
  }),
}));

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
