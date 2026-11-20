// The trigger editor offers a tab for each polled event an enabled capa
// declares -- and none without such a capa.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TriggerDTO, TriggerEventDTO } from "@/lib/hooks";

const m = vi.hoisted(() => ({
  events: [] as TriggerEventDTO[],
  triggers: [] as TriggerDTO[],
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: vi.fn(() => Promise.resolve([])), post: vi.fn() } };
});

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useTriggerEvents: () => ({ data: m.events }),
    useAgentTriggers: () => ({ data: m.triggers }),
  };
});

vi.mock("@/lib/connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/connections")>();
  return { ...actual, useAgentToolAccess: () => ({ data: [] }) };
});

vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return { ...actual, Link: ({ children }: { children: ReactNode }) => <a>{children}</a> };
});

import { TriggerEditor } from "@/routes/agents.$id";

const MAIL: TriggerEventDTO = {
  source: "email",
  type: "mail.received",
  label: "New email",
  labelTranslations: {},
  description: "",
  descriptionTranslations: {},
  polled: true,
  toolKey: "email",
  config: [],
};

function editor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TriggerEditor initial="" agentName="Ada" agentId="a1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  m.events = [];
  m.triggers = [];
});

describe("trigger editor tabs", () => {
  it("has no polled event tab without a capa that declares one", () => {
    m.events = [{ ...MAIL, polled: false, toolKey: null }];
    editor();

    expect(screen.getByRole("button", { name: "Schedule" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Webhook" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "On new email" })).toBeNull();
  });

  it("offers the polled event's tab and opens it", () => {
    m.events = [MAIL];
    editor();

    fireEvent.click(screen.getByRole("button", { name: "On new email" }));

    expect(screen.getByText("Give this agent a mailbox first")).toBeInTheDocument();
  });

  it("opens on the polled event's tab when the agent has that trigger", () => {
    m.events = [MAIL];
    m.triggers = [
      {
        id: "t1",
        agentId: "a1",
        kind: "event",
        taskText: "x",
        enabled: true,
        cronExpression: null,
        nextRunAt: null,
        lastRunAt: null,
        eventSource: "email",
        eventType: "mail.received",
        webhookUrl: null,
        createdByMemberId: null,
        createdByName: null,
        actsForCreator: false,
        needsConfirmation: false,
      },
    ];
    editor();

    expect(screen.getByRole("button", { name: "On new email" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
