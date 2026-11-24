// frontend/src/test/agent-private-content-notice.test.tsx
// A personal agent read by someone other than its owner comes back with
// `contentHidden` and blank content: the Instructions and Versions tabs show
// this notice instead of an empty editor that reads as "no instructions".
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: vi.fn(() => Promise.resolve([])), post: vi.fn() } };
});

import { PrivateContentNotice } from "@/routes/agents.$id";

describe("private content notice", () => {
  it("says the content belongs to the owner, without an editor", () => {
    render(<PrivateContentNotice />);
    expect(screen.getByText(/only its owner sees its instructions/i)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
