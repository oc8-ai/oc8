import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { granted, getMock, postMock, toastErrorMock, toastSuccessMock } = vi.hoisted(() => ({
  granted: { current: new Set<string>() },
  getMock: vi.fn(),
  postMock: vi.fn(),
  toastErrorMock: vi.fn(),
  toastSuccessMock: vi.fn(),
}));

vi.mock("@/lib/governance-hooks", () => ({
  useMay: () => (p: string) => granted.current.has(p),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: getMock, post: postMock } };
});
vi.mock("sonner", () => ({ toast: { error: toastErrorMock, success: toastSuccessMock } }));
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({ children, params }: { children: React.ReactNode; params?: { id: string } }) => (
      <a href={`/agents/${params?.id}`}>{children}</a>
    ),
  };
});

import { ApiError } from "@/lib/api";
import { PersonalAssistantsPanel } from "@/components/personal-assistants-panel";

const DEPT = { id: "d1", name: "Ada's assistants", kind: "personal" };
const TEMPLATES: unknown[] = [
  {
    capaId: "c1",
    name: "helper",
    label: "Helper",
    summary: "Helps you",
    hireToken: "tok1",
    grants: { tools: [], skills: [] },
    connectionsAny: [],
    connectionsOptional: [],
  },
];

let mine: { enabled: boolean; department: unknown };
let agents: unknown[];

beforeEach(() => {
  granted.current = new Set(["department:own"]);
  mine = { enabled: true, department: null };
  agents = [];
  getMock.mockReset();
  postMock.mockReset();
  toastErrorMock.mockReset();
  toastSuccessMock.mockReset();
  getMock.mockImplementation((path: string) => {
    if (path === "/personal-department") return Promise.resolve(mine);
    if (path === "/personal-department/templates") return Promise.resolve(TEMPLATES);
    if (path.startsWith("/agents")) return Promise.resolve({ items: agents, totalCount: 1 });
    return Promise.resolve([]);
  });
  postMock.mockResolvedValue({ agentId: "a9", departmentId: "d1" });
});

function renderPanel(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={qc}>
      <PersonalAssistantsPanel />
    </QueryClientProvider>,
  );
}

describe("PersonalAssistantsPanel", () => {
  it("lists personal templates and hires one", async () => {
    renderPanel();
    expect(await screen.findByText("Helper")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /hire/i }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/personal-department/agents", {
        capaId: "c1",
        hireToken: "tok1",
      }),
    );
    await waitFor(() => expect(toastSuccessMock).toHaveBeenCalled());
  });

  it("opens the hire dialog for a template that connects to a login", async () => {
    TEMPLATES.push({
      capaId: "c2",
      name: "mail",
      label: "Mail helper",
      summary: "",
      hireToken: "tok2",
      grants: { tools: ["email"], skills: [] },
      connectionsAny: [
        {
          integration: "email",
          label: "Email",
          kind: "credential",
          provider: null,
          credentialType: "mail_account",
          available: true,
        },
      ],
      connectionsOptional: [],
    });
    try {
      renderPanel();
      await screen.findByText("Mail helper");
      fireEvent.click(screen.getAllByRole("button", { name: /hire/i })[1]);
      expect(
        await screen.findByLabelText(/let this assistant use my account/i),
      ).toBeInTheDocument();
      expect(postMock).not.toHaveBeenCalled();
    } finally {
      TEMPLATES.pop();
    }
  });

  it("refreshes the admin's personal-department list after a hire", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(qc, "invalidateQueries");
    renderPanel(qc);
    fireEvent.click(await screen.findByRole("button", { name: /hire/i }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ["personal-departments"] }));
  });

  it("lists my personal agents linking to their pages", async () => {
    mine = { enabled: true, department: DEPT };
    agents = [{ id: "a1", name: "Pia" }];
    renderPanel();
    const link = (await screen.findByText("Pia")).closest("a");
    expect(link).toHaveAttribute("href", "/agents/a1");
    expect(getMock).toHaveBeenCalledWith(expect.stringContaining("departmentId=d1"));
  });

  it("says personal departments are off and offers no hire", async () => {
    mine = { enabled: false, department: DEPT };
    agents = [{ id: "a1", name: "Pia" }];
    renderPanel();
    expect(await screen.findByText("Pia")).toBeInTheDocument();
    expect(screen.getByText(/new hires are (turned )?off/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /hire/i })).toBeNull();
  });

  it("renders nothing without department:own", async () => {
    granted.current = new Set();
    const { container } = renderPanel();
    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
      expect(getMock).not.toHaveBeenCalled();
    });
  });

  it("disables the button while hiring", async () => {
    postMock.mockReturnValue(new Promise(() => {}));
    renderPanel();
    const btn = await screen.findByRole("button", { name: /hire/i });
    fireEvent.click(btn);
    await waitFor(() => expect(btn).toBeDisabled());
  });

  it.each([
    [409, { error: "personal_agent_limit" }, /maximum number of personal assistants/i],
    [403, { error: "personal_departments_disabled" }, /turned off/i],
    [404, "plugin not enabled", /not available/i],
    [422, { error: "template_not_personal" }, /not available/i],
    [400, "tools outside the frame", /organisation's rules/i],
    [500, "boom", /couldn't hire/i],
  ])("maps a %s hire error to a readable toast", async (status, detail, text) => {
    const body = { detail };
    postMock.mockRejectedValue(
      new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), status, body),
    );
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /hire/i }));
    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledWith(expect.stringMatching(text)));
  });
});
