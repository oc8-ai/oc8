import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

const upload = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  uploadChatAttachment: upload,
}));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({
    data: [{ key: "plan", summary: "Plan", allowsTools: true, allowsWrites: false }],
  }),
}));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => null,
  useCan: () => () => true,
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useContextSearch: () => [{ kind: "knowledge_base", id: "kb1", label: "Handbook" }],
  // A model that reasons, so the effort chip shows too.
  useSessionModels: () => ({
    data: [
      {
        id: null,
        label: "Auto",
        provider: "",
        locality: "",
        isDefault: false,
        supportsReasoning: true,
      },
    ],
    isLoading: false,
  }),
  useSetSessionModel: () => ({ mutate: vi.fn() }),
  useSetSessionEffort: () => ({ mutate: vi.fn() }),
}));

import { Composer, type ComposerProps } from "@/components/conversation/composer";

function Harness(props: Partial<ComposerProps>) {
  const [draft, setDraft] = useState("");
  return (
    <QueryClientProvider client={new QueryClient()}>
      <Composer
        sessionId="s"
        ensureSession={async () => "s"}
        draft={draft}
        onDraftChange={setDraft}
        busy={false}
        onSend={vi.fn()}
        onStop={vi.fn()}
        modelValue={null}
        showModelPicker={false}
        {...props}
      />
    </QueryClientProvider>
  );
}

// Phones get 44 px touch targets on every composer control (spec §5.7);
// from sm up the pointer-sized 36 px stay.
const TOUCH = /(^|\s)(size-11|h-11)(\s|$)/;
const POINTER = /(^|\s)sm:(size-9|h-9)(\s|$)/;

describe("Composer on a phone", () => {
  it("gives every control in the bottom row a 44 px touch target", () => {
    render(<Harness showModelPicker web={{ on: false, onChange: vi.fn() }} />);
    const controls = [
      screen.getByRole("button", { name: /Add files or context/ }),
      screen.getByRole("button", { name: /^Model:/ }),
      screen.getByRole("button", { name: /^Reasoning:/ }),
      screen.getByRole("button", { name: /Web/ }),
      screen.getByRole("button", { name: /^Send$/ }),
    ];
    for (const control of controls) {
      expect(control.className, control.getAttribute("aria-label") ?? "").toMatch(TOUCH);
      expect(control.className, control.getAttribute("aria-label") ?? "").toMatch(POINTER);
    }
  });
});
