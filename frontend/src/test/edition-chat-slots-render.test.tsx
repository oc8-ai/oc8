// Spec §8 invariant 11: the render points of the chat slots.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { Star } from "lucide-react";
import { useState, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const caps = vi.hoisted(() => ({ value: null as string[] | null }));
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => caps.value,
  useCan: () => () => true,
}));
vi.mock("@/lib/chat-commands", async (orig) => ({
  ...(await orig<typeof import("@/lib/chat-commands")>()),
  useChatModes: () => ({ data: [] }),
}));
vi.mock("@/lib/conversation-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/conversation-api")>()),
  useContextSearch: () => [],
  useSessionModels: () => ({ data: [] }),
  useSetSessionModel: () => ({ mutate: vi.fn() }),
  useTurnRun: () => ({ data: undefined }),
  useMessageFeedback: () => ({ mutate: vi.fn() }),
}));

import { AssistantTurn } from "@/components/conversation/assistant-turn";
import { Composer, type ComposerProps } from "@/components/conversation/composer";
import { EditionCompositionProvider } from "@/edition/composition";
import type { FrontendEditionExtension } from "@/edition/contracts";
import type { ChatMessageDTO } from "@/lib/hooks-chat";

const onSelect = vi.fn();
const ext: FrontendEditionExtension = {
  id: "ent",
  capabilities: ["enterprise.dictation"],
  chat: {
    composerActions: [
      { id: "mic", needs: ["enterprise.dictation"], component: () => <button>Dictate</button> },
    ],
    composerPickers: [
      {
        id: "web",
        label: { en: "Web search", de: "Websuche" },
        icon: Star,
        command: "web",
        onSelect,
      },
    ],
    messageParts: [{ id: "sources", type: "sources", component: () => <p>edition sources</p> }],
  },
};

function Harness(props: Partial<ComposerProps>) {
  const [draft, setDraft] = useState("");
  return (
    <QueryClientProvider client={new QueryClient()}>
      <EditionCompositionProvider extensions={[ext]}>
        <Composer
          sessionId="s"
          ensureSession={async () => "s"}
          draft={draft}
          onDraftChange={setDraft}
          busy={false}
          onSend={props.onSend ?? vi.fn()}
          onStop={vi.fn()}
          modelValue={null}
          showModelPicker={false}
          {...props}
        />
      </EditionCompositionProvider>
    </QueryClientProvider>
  );
}

describe("composer slots", () => {
  it("renders an action whose capability the declaration covers while capabilities load", () => {
    caps.value = null;
    render(<Harness />);
    expect(screen.getByRole("button", { name: "Dictate" })).toBeInTheDocument();
  });

  it("hides an action once the known capabilities lack it", () => {
    caps.value = [];
    render(<Harness />);
    expect(screen.queryByRole("button", { name: "Dictate" })).toBeNull();
  });

  it("hides actions and edition pickers in a read-only chat", () => {
    caps.value = ["enterprise.dictation"];
    render(<Harness disabled />);
    expect(screen.queryByRole("button", { name: "Dictate" })).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: /Message/ }), {
      target: { value: "/we" },
    });
    expect(screen.queryByText("/web")).toBeNull();
  });

  it("runs a picker's /command locally instead of sending it", () => {
    caps.value = ["enterprise.dictation"];
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    const box = screen.getByRole("textbox", { name: /Message/ });
    fireEvent.change(box, { target: { value: "/we" } });
    expect(screen.getByText("/web")).toBeInTheDocument();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSend).not.toHaveBeenCalled();
    expect(box).toHaveValue("");

    fireEvent.change(box, { target: { value: "/web" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSend).not.toHaveBeenCalled();
  });
});

const message: ChatMessageDTO = {
  id: "m",
  sessionId: "s",
  role: "assistant",
  content: "Answer [1]",
  runId: null,
  renderedComponents: [],
  createdAt: "2026-11-08T09:00:00Z",
  attachments: [],
  mode: null,
  contextRefs: [],
  authorMemberId: null,
  authorName: null,
  replyToId: "u1",
  parts: [
    {
      type: "sources",
      items: [
        { n: 1, title: "Doc", url: "https://a.example", favicon: null, snippet: "", kind: "web" },
      ],
    },
  ],
};

function turn(extensions: readonly FrontendEditionExtension[]): ReactNode {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <EditionCompositionProvider extensions={extensions}>
        <AssistantTurn
          sessionId="s"
          turn={{
            kind: "assistant",
            key: "k",
            replyToId: "u1",
            variants: [message],
            liveRunId: null,
          }}
        />
      </EditionCompositionProvider>
    </QueryClientProvider>
  );
}

describe("sources suppression", () => {
  it("keeps the core Sources list without an edition sources renderer", () => {
    caps.value = [];
    render(turn([]));
    expect(screen.getByRole("region", { name: "Sources" })).toBeInTheDocument();
    expect(screen.queryByText("edition sources")).toBeNull();
  });

  it("drops the core list when an edition renders sources", () => {
    caps.value = null;
    render(turn([ext]));
    expect(screen.queryByRole("region", { name: "Sources" })).toBeNull();
    expect(screen.getByText("edition sources")).toBeInTheDocument();
  });
});
