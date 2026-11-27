import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getMock, postMock, connectOpen } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  connectOpen: vi.fn(),
}));

vi.mock("@/lib/governance-hooks", () => ({ useMay: () => () => true }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: getMock, post: postMock } };
});
vi.mock("@/components/connections/connect-dialog", () => ({
  ConnectDialog: ({ integration, open }: { integration: string; open: boolean }) => {
    if (open) connectOpen(integration);
    return open ? <div>connect-dialog:{integration}</div> : null;
  },
}));

import { ApiError } from "@/lib/api";
import { errorCode } from "@/lib/hire-error";
import { PersonalHireDialog } from "@/components/personal-hire-dialog";
import type { PersonalTemplateDTO } from "@/lib/hooks";

const EMAIL = {
  integration: "email",
  label: "Email",
  kind: "credential",
  provider: null,
  credentialType: "mail_account",
  available: true,
};
const base: PersonalTemplateDTO = {
  capaId: "c1",
  name: "mail",
  label: "Mail helper",
  summary: "Triages mail",
  hireToken: "tok1",
  grants: { tools: ["email", "calendar"], skills: ["triage"] },
  connectionsAny: [EMAIL],
  connectionsOptional: [],
};
const LOGIN = {
  id: "l1",
  kind: "credential",
  integration: "email",
  provider: null,
  credentialType: "mail_account",
  accountLabel: "ada@x",
  owner: "me",
  status: "ok",
};
const CONSENT = /let this assistant use my account/i;

let mine: unknown[];

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
  connectOpen.mockReset();
  mine = [LOGIN];
  getMock.mockImplementation((path: string) => {
    if (path === "/connections/mine") return Promise.resolve(mine);
    if (path === "/connections/integrations") return Promise.resolve([]);
    return Promise.resolve([]);
  });
  postMock.mockResolvedValue({ agentId: "a9", departmentId: "d1" });
});

function renderDialog(tpl: PersonalTemplateDTO = base, onOpenChange = vi.fn(), qc?: QueryClient) {
  const client = qc ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PersonalHireDialog template={tpl} open onOpenChange={onOpenChange} />
    </QueryClientProvider>,
  );
  return { onOpenChange, client };
}

const hireButton = () => screen.getByRole("button", { name: /^hire$|^einstellen$/i });

describe("PersonalHireDialog", () => {
  it("shows what the assistant gets", async () => {
    renderDialog();
    expect(await screen.findByText("calendar")).toBeInTheDocument();
    expect(screen.getByText("triage")).toBeInTheDocument();
  });

  it("posts the chosen own login with consent", async () => {
    renderDialog();
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    fireEvent.click(hireButton());
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/personal-department/agents", {
        capaId: "c1",
        hireToken: "tok1",
        connection: { integration: "email", credentialId: "l1" },
        optionalConnections: [],
        confirmShare: true,
      }),
    );
  });

  it("sends an OAuth login as oauthConnectionId", async () => {
    mine = [
      {
        ...LOGIN,
        id: "o1",
        kind: "oauth",
        integration: "microsoft365",
        provider: "microsoft",
        accountLabel: "ada@ms",
        status: "active",
      },
    ];
    renderDialog({
      ...base,
      connectionsAny: [
        { ...EMAIL, integration: "microsoft365", kind: "oauth", provider: "microsoft" },
      ],
    });
    fireEvent.click(await screen.findByLabelText(/ada@ms/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    fireEvent.click(hireButton());
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(
        "/personal-department/agents",
        expect.objectContaining({
          connection: { integration: "microsoft365", oauthConnectionId: "o1" },
        }),
      ),
    );
  });

  it("keeps Hire disabled without consent", async () => {
    renderDialog();
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    expect(hireButton()).toBeDisabled();
    fireEvent.click(screen.getByLabelText(CONSENT));
    expect(hireButton()).toBeEnabled();
  });

  it("keeps Hire disabled until a login is chosen", async () => {
    mine = [LOGIN, { ...LOGIN, id: "l2", accountLabel: "bob@x" }];
    renderDialog();
    fireEvent.click(await screen.findByLabelText(CONSENT));
    expect(hireButton()).toBeDisabled();
  });

  it("does not offer a failed login", async () => {
    mine = [{ ...LOGIN, status: "failed" }];
    renderDialog();
    expect(await screen.findByRole("button", { name: /connect a new one/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/ada@x/)).toBeNull();
  });

  it("marks an unavailable alternative and never offers it", async () => {
    renderDialog({
      ...base,
      connectionsAny: [
        { ...EMAIL, available: false },
        { ...EMAIL, integration: "gmail", label: "Gmail", kind: "oauth", provider: "google" },
      ],
    });
    const unavailable = await screen.findByLabelText(/^Email/);
    expect(unavailable).toBeDisabled();
    expect(screen.getByText(/not available/i)).toBeInTheDocument();
    // The only available alternative is preselected.
    expect(screen.getByLabelText(/^Gmail/)).toBeChecked();
  });

  it("offers connecting a new login when the member has none", async () => {
    mine = [];
    renderDialog();
    fireEvent.click(await screen.findByRole("button", { name: /connect a new one/i }));
    expect(await screen.findByText("connect-dialog:email")).toBeInTheDocument();
  });

  it("sends a ticked optional login", async () => {
    mine = [LOGIN, { ...LOGIN, id: "n1", integration: "notes", accountLabel: "notes@x" }];
    renderDialog({
      ...base,
      connectionsOptional: [{ ...EMAIL, integration: "notes", label: "Notes" }],
    });
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(/^Notes/));
    fireEvent.click(await screen.findByLabelText(/notes@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    fireEvent.click(hireButton());
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(
        "/personal-department/agents",
        expect.objectContaining({
          optionalConnections: [{ integration: "notes", credentialId: "n1" }],
        }),
      ),
    );
  });

  it("shows the server's reason for a refused login", async () => {
    postMock.mockRejectedValue(new ApiError("x", 422, { detail: { error: "login_not_usable" } }));
    renderDialog();
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    fireEvent.click(hireButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't be used|cannot be used/i);
  });

  it("refetches the templates and stays open on hire_changed", async () => {
    postMock.mockRejectedValue(new ApiError("x", 409, { detail: { error: "hire_changed" } }));
    const { onOpenChange, client } = renderDialog();
    const spy = vi.spyOn(client, "invalidateQueries");
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    fireEvent.click(hireButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/changed/i);
    expect(spy).toHaveBeenCalledWith({ queryKey: ["personal-department", "templates"] });
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("shows the incomplete-login reason from a {code} body", async () => {
    postMock.mockRejectedValue(
      new ApiError("x", 422, { detail: { code: "credential_incomplete", missing: ["host"] } }),
    );
    renderDialog();
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    fireEvent.click(hireButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/missing details/i);
    expect(errorCode(new ApiError("x", 422, { detail: { code: "a" } }))).toBe("a");
  });

  it("does not hire an alternative that became unavailable", async () => {
    const { rerender } = render(
      <QueryClientProvider client={new QueryClient()}>
        <PersonalHireDialog template={base} open onOpenChange={vi.fn()} />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    expect(hireButton()).toBeEnabled();
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <PersonalHireDialog
          template={{ ...base, connectionsAny: [{ ...EMAIL, available: false }] }}
          open
          onOpenChange={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(hireButton()).toBeDisabled();
  });

  it("resets consent for a new offer and sends the new token", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ui = (tpl: PersonalTemplateDTO) => (
      <QueryClientProvider client={client}>
        <PersonalHireDialog template={tpl} open onOpenChange={vi.fn()} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui(base));
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    rerender(ui({ ...base, hireToken: "tok2" }));
    expect(screen.getByLabelText(CONSENT)).not.toBeChecked();
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    fireEvent.click(hireButton());
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(
        "/personal-department/agents",
        expect.objectContaining({ hireToken: "tok2" }),
      ),
    );
  });

  it("asks consent without a login section for a template with grants only", async () => {
    renderDialog({ ...base, connectionsAny: [] });
    fireEvent.click(await screen.findByLabelText(CONSENT));
    expect(screen.queryByRole("button", { name: /connect a new one/i })).toBeNull();
    fireEvent.click(hireButton());
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/personal-department/agents", {
        capaId: "c1",
        hireToken: "tok1",
        confirmShare: true,
      }),
    );
  });

  it("closes after a successful hire", async () => {
    const { onOpenChange } = renderDialog();
    fireEvent.click(await screen.findByLabelText(/ada@x/));
    fireEvent.click(screen.getByLabelText(CONSENT));
    fireEvent.click(hireButton());
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });
});
