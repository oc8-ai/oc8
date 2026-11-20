// The trigger editor's tab for a polled event ("On new email"): it listens on
// the agent's fixed login, takes the event's declared settings and the task,
// and shows how polling goes.
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentToolAccess } from "@/lib/connections";
import type { TriggerDTO, TriggerEventDTO } from "@/lib/hooks";

const m = vi.hoisted(() => ({
  access: [] as AgentToolAccess[],
  create: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
}));

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useCreateAgentTrigger: () => ({ mutate: m.create, isPending: false }),
    useUpdateAgentTrigger: () => ({ mutate: m.update, isPending: false }),
    useDeleteAgentTrigger: () => ({ mutate: m.remove, isPending: false }),
    useAuth: () => ({ data: { memberId: "me" } }),
  };
});

vi.mock("@/lib/connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/connections")>();
  return { ...actual, useAgentToolAccess: () => ({ data: m.access }) };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@tanstack/react-router", () => ({
  Link: ({
    children,
    to,
    search,
  }: {
    children: ReactNode;
    to: string;
    search?: Record<string, string>;
  }) => <a href={`${to}?${new URLSearchParams(search).toString()}`}>{children}</a>,
}));

import { PolledEventTriggerPanel, polledEventTabLabels } from "@/components/polled-event-trigger";

const EVENT: TriggerEventDTO = {
  source: "email",
  type: "mail.received",
  label: "New email",
  labelTranslations: { de: "Neue E-Mail" },
  description: "",
  descriptionTranslations: {},
  polled: true,
  toolKey: "email",
  editorLabel: "",
  editorLabelTranslations: {},
  sourceLabel: "Email",
  sourceLabelTranslations: {},
  config: [
    {
      key: "folder",
      label: "Folder",
      labelTranslations: {},
      kind: "text",
      default: "INBOX",
      defaultFrom: "",
    },
  ],
};

const MAILBOX: AgentToolAccess = {
  toolKey: "email",
  integration: "email",
  label: "Email",
  kind: "credential",
  mode: "fixed",
  defaultMode: "organisation",
  oauthConnectionId: null,
  credentialId: "cred-1",
  targetLabel: "einkauf@acme.test",
  targetOwnerName: null,
  targetMissing: false,
  needsConfirmation: false,
  confirmationReason: null,
};

const TRIGGER: TriggerDTO = {
  id: "t1",
  agentId: "a1",
  kind: "event",
  taskText: "Sort the order mail.",
  enabled: true,
  cronExpression: null,
  nextRunAt: null,
  lastRunAt: null,
  eventSource: "email",
  eventType: "mail.received",
  webhookUrl: null,
  createdByMemberId: "me",
  createdByName: "Me",
  actsForCreator: false,
  needsConfirmation: false,
  eventTargetCredentialId: "cred-1",
  eventConfig: { folder: "Orders" },
  pollStatus: { lastPolledAt: null, lastOkAt: null, lastError: null, lastErrorAt: null },
  lastSkipReason: null,
};

function panel(trigger?: TriggerDTO) {
  return render(
    <PolledEventTriggerPanel agentId="a1" agentName="Ada" event={EVENT} trigger={trigger} />,
  );
}

const when = (iso: string) =>
  new Date(iso).toLocaleString("en-US", { dateStyle: "short", timeStyle: "short" });

beforeEach(() => {
  m.access = [MAILBOX];
  m.create.mockReset();
  m.update.mockReset();
  m.remove.mockReset();
});

describe("polled event trigger", () => {
  it("names the tab after the event, and tells equal names apart", () => {
    const en = (s: string) => s;
    const named = {
      ...EVENT,
      editorLabel: "On new email",
      editorLabelTranslations: { de: "Bei neuer E-Mail" },
    };
    expect(polledEventTabLabels([named], en, "en")).toEqual(["On new email"]);
    expect(polledEventTabLabels([named], en, "de")).toEqual(["Bei neuer E-Mail"]);
    // Without its own tab name: the event's label.
    expect(polledEventTabLabels([EVENT], en, "de")).toEqual(["Neue E-Mail"]);
    const other = { ...named, source: "microsoft365", sourceLabel: "Microsoft 365" };
    expect(polledEventTabLabels([named, other], en, "en")).toEqual([
      "On new email (Email)",
      "On new email (Microsoft 365)",
    ]);
  });

  it("asks for a mailbox first when the agent has none", () => {
    m.access = [{ ...MAILBOX, mode: "user", credentialId: null, targetLabel: null }];
    panel();

    expect(screen.getByText("Give this agent a mailbox first")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /tool settings/i })).toHaveAttribute(
      "href",
      "/agents/$id?tab=guardrails",
    );
    expect(screen.queryByRole("button", { name: /save/i })).toBeNull();
  });

  it("shows the agent's mailbox read-only with a link to change it", () => {
    panel();

    expect(screen.getByText("einkauf@acme.test")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("einkauf@acme.test")).toBeNull();
    expect(screen.getByRole("link", { name: /tool settings/i })).toBeInTheDocument();
  });

  it("creates the trigger with the folder and the task text", () => {
    panel();

    expect(screen.getByLabelText("Folder")).toHaveValue("INBOX");
    expect(screen.getByText(/email is appended/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "Orders" } });
    fireEvent.change(screen.getByLabelText("Task"), { target: { value: "File the orders." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(m.create).toHaveBeenCalledWith(
      {
        agentId: "a1",
        kind: "event",
        eventSource: "email",
        eventType: "mail.received",
        eventConfig: { folder: "Orders" },
        taskText: "File the orders.",
      },
      expect.anything(),
    );
  });

  it("saves changes to an existing trigger on the agent's current mailbox", () => {
    panel(TRIGGER);

    expect(screen.getByLabelText("Folder")).toHaveValue("Orders");
    expect(screen.getByLabelText("Task")).toHaveValue("Sort the order mail.");
    fireEvent.change(screen.getByLabelText("Folder"), { target: { value: "Archive" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(m.update).toHaveBeenCalledWith(
      {
        agentId: "a1",
        triggerId: "t1",
        eventConfig: { folder: "Archive" },
        taskText: "Sort the order mail.",
        eventTargetCredentialId: "cred-1",
      },
      expect.anything(),
    );
  });

  it("switches the trigger on and off", () => {
    panel({ ...TRIGGER, enabled: false });

    fireEvent.click(screen.getByRole("switch", { name: /listening/i }));

    expect(m.update).toHaveBeenCalledWith(
      { agentId: "a1", triggerId: "t1", enabled: true, eventTargetCredentialId: "cred-1" },
      expect.anything(),
    );
  });

  it("shows the last poll and the error with its time", () => {
    panel({
      ...TRIGGER,
      pollStatus: {
        lastPolledAt: "2026-11-20T10:45:00+00:00",
        lastOkAt: "2026-11-20T09:00:00+00:00",
        lastError: "LoginFailed: login failed",
        lastErrorAt: "2026-11-20T10:42:00+00:00",
      },
    });

    expect(
      screen.getByText(new RegExp(`Last checked ${when("2026-11-20T10:45:00Z")}`)),
    ).toBeInTheDocument();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("login failed");
    expect(alert).toHaveTextContent(when("2026-11-20T10:42:00Z"));
    expect(alert).toHaveTextContent(when("2026-11-20T09:00:00Z"));
  });

  it("hides an error a later successful poll cleared", () => {
    panel({
      ...TRIGGER,
      pollStatus: {
        lastPolledAt: "2026-11-20T11:00:00+00:00",
        lastOkAt: "2026-11-20T11:00:00+00:00",
        lastError: "LoginFailed: login failed",
        lastErrorAt: "2026-11-20T10:42:00+00:00",
      },
    });

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says when it was switched off because the mailbox changed", () => {
    panel({ ...TRIGGER, enabled: false, lastSkipReason: "event_target_changed" });

    const status = screen.getByRole("status");
    expect(within(status).getByText(/Mailbox changed/)).toBeInTheDocument();
  });
});
