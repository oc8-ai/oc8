import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentToolAccess } from "@/lib/connections";
import { LanguageProvider } from "@/lib/i18n";

const access = vi.fn();
const options = vi.fn();
const setAccess = vi.fn();
const reset = vi.fn();

vi.mock("@/lib/connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/connections")>();
  return {
    ...actual,
    useIntegrations: () => ({ data: [] }),
    useAgentToolAccess: () => access(),
    useAgentToolAccessOptions: () => options(),
    useSetAgentToolAccess: () => ({ mutateAsync: setAccess, isPending: false }),
    useResetAgentToolAccess: () => ({ mutateAsync: reset, isPending: false }),
  };
});

import { AgentToolAccessPanel } from "@/components/connections/agent-tool-access";

function renderPanel(mayManage = true) {
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <LanguageProvider>
        <AgentToolAccessPanel agentId="a1" toolKeys={["microsoft365"]} mayManage={mayManage} />
      </LanguageProvider>
    </QueryClientProvider>,
  );
}

const MS_ROW: AgentToolAccess = {
  toolKey: "microsoft365",
  integration: "microsoft365",
  label: "Microsoft 365",
  kind: "oauth",
  mode: "default",
  defaultMode: "user",
  oauthConnectionId: null,
  credentialId: null,
  targetLabel: null,
  targetOwnerName: null,
  targetMissing: false,
  needsConfirmation: false,
  confirmationReason: null,
};

function rows(ms: Partial<AgentToolAccess> = {}) {
  return {
    data: [
      { ...MS_ROW, ...ms },
      {
        toolKey: "odoo",
        integration: "odoo_mcp",
        label: "Odoo",
        kind: "credential",
        mode: "default",
        defaultMode: "organisation",
        oauthConnectionId: null,
        credentialId: null,
        targetLabel: null,
        targetOwnerName: null,
        targetMissing: false,
        needsConfirmation: false,
        confirmationReason: null,
      },
    ],
  };
}

describe("AgentToolAccessPanel", () => {
  beforeEach(() => {
    window.localStorage.setItem("oc8-lang", "de");
    access.mockReturnValue(rows());
    options.mockReturnValue({
      data: [
        { kind: "user", id: null, label: "user", owner: null, ownerName: null },
        {
          kind: "oauth",
          id: "o1",
          label: "shared@acme.de",
          owner: "organisation",
          ownerName: null,
        },
        { kind: "oauth", id: "m1", label: "me@acme.de", owner: "me", ownerName: "Ich" },
        { kind: "oauth", id: "p1", label: "anna@acme.de", owner: "member", ownerName: "Anna" },
      ],
    });
    setAccess.mockReset();
    setAccess.mockResolvedValue({});
    reset.mockReset();
  });

  it("shows only the agent's enabled tools, with the schedule hint", async () => {
    renderPanel();
    expect(await screen.findByLabelText("Zugang für Microsoft 365")).toBeInTheDocument();
    expect(screen.queryByLabelText("Zugang für Odoo")).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Bei geplanten Läufen wird das Konto der Person genutzt, die den Zeitplan angelegt hat.",
      ),
    ).toBeInTheDocument();
  });

  it("offers the user's context, organisation accesses and personal accounts", async () => {
    renderPanel();
    const select = await screen.findByLabelText("Zugang für Microsoft 365");
    const labels = Array.from((select as HTMLSelectElement).options).map((o) => o.textContent);
    expect(labels).toEqual([
      "Standard: im Kontext des Nutzers",
      "Im Kontext des Nutzers",
      "Organisation: shared@acme.de",
      "Ihr Konto: me@acme.de",
      "Konto von Anna: anna@acme.de",
    ]);
  });

  it("warns before binding another person's account", async () => {
    renderPanel();
    fireEvent.change(await screen.findByLabelText("Zugang für Microsoft 365"), {
      target: { value: "oauth:p1" },
    });
    expect(await screen.findByText(/handeln dann mit dem Konto von Anna/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Trotzdem festlegen" }));
    await waitFor(() =>
      expect(setAccess).toHaveBeenCalledWith({
        toolKey: "microsoft365",
        body: { mode: "fixed", oauthConnectionId: "p1" },
      }),
    );
  });

  it("shares your own account only after you confirm it", async () => {
    renderPanel();
    fireEvent.change(await screen.findByLabelText("Zugang für Microsoft 365"), {
      target: { value: "oauth:m1" },
    });
    expect(
      await screen.findByText(
        "Wenn Kolleginnen und Kollegen mit diesem Agenten arbeiten, nutzt er dafür Ihr Konto me@acme.de.",
      ),
    ).toBeInTheDocument();
    expect(setAccess).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Konto freigeben" }));
    await waitFor(() =>
      expect(setAccess).toHaveBeenCalledWith({
        toolKey: "microsoft365",
        body: { mode: "fixed", oauthConnectionId: "m1", confirmShare: true },
      }),
    );
  });

  it("does not share your own account when you cancel", async () => {
    renderPanel();
    fireEvent.change(await screen.findByLabelText("Zugang für Microsoft 365"), {
      target: { value: "oauth:m1" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Abbrechen" }));
    await waitFor(() =>
      expect(screen.queryByText(/nutzt er dafür Ihr Konto/)).not.toBeInTheDocument(),
    );
    expect(setAccess).not.toHaveBeenCalled();
  });

  it("sets an organisation access and the user's context directly", async () => {
    renderPanel();
    const select = await screen.findByLabelText("Zugang für Microsoft 365");
    fireEvent.change(select, { target: { value: "oauth:o1" } });
    await waitFor(() =>
      expect(setAccess).toHaveBeenCalledWith({
        toolKey: "microsoft365",
        body: { mode: "fixed", oauthConnectionId: "o1" },
      }),
    );
    fireEvent.change(select, { target: { value: "user" } });
    await waitFor(() =>
      expect(setAccess).toHaveBeenCalledWith({ toolKey: "microsoft365", body: { mode: "user" } }),
    );
    fireEvent.change(select, { target: { value: "default" } });
    await waitFor(() => expect(reset).toHaveBeenCalledWith("microsoft365"));
  });

  it("asks the owner again before re-confirming their own binding", async () => {
    access.mockReturnValue(
      rows({
        mode: "fixed",
        oauthConnectionId: "m1",
        targetLabel: "me@acme.de",
        targetOwnerName: "Ich",
        needsConfirmation: true,
        confirmationReason: "agent_changed",
      }),
    );
    renderPanel();
    expect(
      await screen.findByText(
        "Der Agent hat sich geändert, seit dieser Zugang bestätigt wurde. Läufe, die ihn brauchen, schlagen fehl, bis er erneut bestätigt ist.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));
    expect(
      await screen.findByText(
        "Wenn Kolleginnen und Kollegen mit diesem Agenten arbeiten, nutzt er dafür Ihr Konto me@acme.de.",
      ),
    ).toBeInTheDocument();
    expect(setAccess).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Konto freigeben" }));
    await waitFor(() =>
      expect(setAccess).toHaveBeenCalledWith({
        toolKey: "microsoft365",
        body: { mode: "fixed", oauthConnectionId: "m1", confirmShare: true },
      }),
    );
  });

  it("sends nothing when the owner cancels the re-confirmation", async () => {
    access.mockReturnValue(
      rows({
        mode: "fixed",
        oauthConnectionId: "m1",
        targetLabel: "me@acme.de",
        targetOwnerName: "Ich",
        needsConfirmation: true,
        confirmationReason: "not_confirmed",
      }),
    );
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Bestätigen" }));
    fireEvent.click(await screen.findByRole("button", { name: "Abbrechen" }));
    await waitFor(() =>
      expect(screen.queryByText(/nutzt er dafür Ihr Konto/)).not.toBeInTheDocument(),
    );
    expect(setAccess).not.toHaveBeenCalled();
  });

  it("tells someone who cannot edit the agent to ask an administrator", async () => {
    access.mockReturnValue(
      rows({
        mode: "fixed",
        oauthConnectionId: "m1",
        targetLabel: "me@acme.de",
        targetOwnerName: "Ich",
        needsConfirmation: true,
        confirmationReason: "agent_changed",
      }),
    );
    renderPanel(false);
    expect(
      await screen.findByText(
        "Sie können diesen Agenten nicht bearbeiten. Bitte wenden Sie sich an eine Administratorin bzw. einen Administrator oder an jemanden, der den Agenten bearbeiten darf.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Bestätigen" })).not.toBeInTheDocument();
  });

  it("lets an administrator confirm a colleague's binding without sharing their own", async () => {
    access.mockReturnValue(
      rows({
        mode: "fixed",
        oauthConnectionId: "p1",
        targetLabel: "anna@acme.de",
        targetOwnerName: "Anna",
        needsConfirmation: true,
        confirmationReason: "not_confirmed",
      }),
    );
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Bestätigen" }));
    await waitFor(() =>
      expect(setAccess).toHaveBeenCalledWith({
        toolKey: "microsoft365",
        body: { mode: "fixed", oauthConnectionId: "p1" },
      }),
    );
  });

  it("explains who can confirm when you cannot", async () => {
    access.mockReturnValue(
      rows({
        mode: "fixed",
        oauthConnectionId: "x9",
        targetLabel: "bob@acme.de",
        targetOwnerName: "Bob",
        needsConfirmation: true,
        confirmationReason: "agent_changed",
      }),
    );
    renderPanel();
    expect(
      await screen.findByText(
        "Bestätigen können der Besitzer des Kontos oder eine Administratorin bzw. ein Administrator.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Bestätigen" })).not.toBeInTheDocument();
  });

  it("is read-only without the right to change the agent", async () => {
    renderPanel(false);
    expect(await screen.findByLabelText("Zugang für Microsoft 365")).toBeDisabled();
  });
});
