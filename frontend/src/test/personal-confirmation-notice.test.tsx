import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { getMock, postMock } = vi.hoisted(() => ({ getMock: vi.fn(), postMock: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { get: getMock, post: postMock } };
});

import { ApiError } from "@/lib/api";
import { PersonalConfirmationNotice } from "@/components/personal-confirmation-notice";

const POLICY = {
  enabled: true,
  read: true,
  modify: false,
  approval_eur: 50,
  approval_actions: [],
  only: ["send_mail"],
  connection_id: null,
  conditions: [],
  limits: [{ tools: ["send_mail"], max: 10, per: "day" }],
};

function summary(over: Record<string, unknown> = {}) {
  return {
    needed: true,
    token: "tk",
    tools: [
      {
        key: "mail",
        applied: POLICY,
        pending: { ...POLICY, modify: true, approval_eur: 500, default_connection_id: "login-9" },
      },
    ],
    skills: [{ name: "triage", description: "Sorts the inbox", status: "new" }],
    agents: [
      {
        agentId: "a1",
        name: "Mailer",
        changes: ["core", "tools", "threshold"],
        approvalThreshold: 25,
        tools: {
          mail: {
            ...POLICY,
            frame_conditions: [{ attribute: "to", operator: "eq", value: "boss", then: "deny" }],
            frame_extra: { default_connection_id: "login-9" },
          },
          calendar: { ...POLICY, enabled: false },
          odd: { enabled: true, unreadable: "[{raw}]" },
        },
        setup: [
          {
            tool: "mail",
            serverUrl: "https://mcp.example.com:8443/mcp",
            transport: "http",
            scopes: ["mail.read"],
            configKeys: ["region", "folder"],
            logins: [{ name: "Ada work", ownedByOwner: true }],
          },
        ],
      },
    ],
    department: {
      goal: "Keep my inbox tidy",
      frame: { language: "en" },
      frameCapaDefaults: { language: "de" },
    },
    ...over,
  };
}

let current: unknown;

beforeEach(() => {
  current = summary();
  getMock.mockReset();
  postMock.mockReset();
  getMock.mockImplementation((path: string) =>
    path === "/personal-department/confirmation" ? Promise.resolve(current) : Promise.resolve([]),
  );
  postMock.mockResolvedValue(summary({ needed: false, tools: [], skills: [], agents: [] }));
});

function renderNotice(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={qc}>
      <PersonalConfirmationNotice enabled />
    </QueryClientProvider>,
  );
}

describe("PersonalConfirmationNotice", () => {
  it("shows what waits, in content, and confirms it with the token", async () => {
    renderNotice();
    const tool = await screen.findByTestId("tool-mail");
    // Before and after, with the differences marked.
    expect(within(tool).getByText("mail")).toBeInTheDocument();
    expect(within(tool).getByText("500")).toBeInTheDocument();
    expect(within(tool).getByText("50")).toBeInTheDocument();
    expect(within(tool).getAllByTestId("changed").length).toBeGreaterThanOrEqual(2);
    expect(within(tool).getByText(/login-9/)).toBeInTheDocument();
    // Skills: name, description, status.
    expect(screen.getByText("triage")).toBeInTheDocument();
    expect(screen.getByText("Sorts the inbox")).toBeInTheDocument();
    expect(
      screen.getByText(/new/i, { selector: "[data-testid=skill-status]" }),
    ).toBeInTheDocument();
    // Agent: name, what changed, threshold, setup without values.
    expect(screen.getByText("Mailer")).toBeInTheDocument();
    expect(screen.getByText(/25/)).toBeInTheDocument();
    expect(screen.getByText(/mcp\.example\.com:8443\/mcp/)).toBeInTheDocument();
    expect(screen.getByText(/region, folder/)).toBeInTheDocument();
    expect(screen.getByText(/Ada work/)).toBeInTheDocument();
    // The agent's tools are open by default and show every field.
    const grants = screen.getByTestId("agent-tools-a1");
    expect(grants).toHaveAttribute("open");
    expect(within(grants).getByText(/login-9/)).toBeInTheDocument();
    expect(within(grants).getByText(/boss/)).toBeInTheDocument();
    expect(within(screen.getByTestId("agent-tool-calendar")).getByText(/off/i)).toBeInTheDocument();
    expect(within(screen.getByTestId("agent-tool-odd")).getByRole("alert")).toHaveTextContent(
      /stays blocked until an administrator fixes them/i,
    );
    // Department goal and the capa defaults.
    expect(screen.getByText(/"language": "de"/)).toBeInTheDocument();
    expect(screen.getByText("Keep my inbox tidy")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /review and confirm/i }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/personal-department/confirm", { token: "tk" }),
    );
  });

  it("never says a tool that grants nothing now allows every action", async () => {
    current = summary({
      tools: [{ key: "files", applied: null, pending: { ...POLICY, only: ["files_list"] } }],
    });
    renderNotice();
    const tool = await screen.findByTestId("tool-files");
    expect(within(tool).getByText("Nothing is granted for it now.")).toBeInTheDocument();
    const only = within(tool).getByText("Only these actions").closest("tr");
    expect(only).not.toBeNull();
    const [, now, after] = Array.from(only!.querySelectorAll("td"));
    expect(now).toHaveTextContent("-");
    expect(now).not.toHaveTextContent(/everything/i);
    expect(after).toHaveTextContent("files_list");
  });

  it("labels the department's frame apart from its goal, and an empty goal as none", async () => {
    current = summary({
      department: { goal: "", frame: { closed: true }, frameCapaDefaults: {} },
    });
    renderNotice();
    const goal = (await screen.findByText("Goal")).nextElementSibling;
    expect(goal).toHaveTextContent("-");
    expect(goal).not.toHaveTextContent(/closed/);
    const frame = screen.getByText("Department frame").nextElementSibling;
    expect(frame).toHaveTextContent(/"closed": true/);
  });

  it("shows a setup's scopes grouped by right, as a tool pack declares them", async () => {
    const base = summary();
    const agent = (base.agents as Record<string, unknown>[])[0];
    const setup = (agent.setup as Record<string, unknown>[])[0];
    current = {
      ...base,
      agents: [
        {
          ...agent,
          setup: [{ ...setup, scopes: { read: ["mail_get"], modify: ["mail_send", "mail_move"] } }],
        },
      ],
    };
    renderNotice();
    const scopes = (await screen.findByText("Scopes")).nextElementSibling;
    expect(scopes).not.toHaveTextContent("[object Object]");
    expect(scopes).toHaveTextContent("read: mail_get; modify: mail_send, mail_move");
  });

  it("renders nothing when nothing waits", async () => {
    current = summary({ needed: false, tools: [], skills: [], agents: [], department: null });
    const { container } = renderNotice();
    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("asks to review again after a 409 and confirms the fresh token", async () => {
    postMock.mockRejectedValueOnce(
      new ApiError("x", 409, { detail: { error: "confirmation_changed" } }),
    );
    renderNotice();
    fireEvent.click(await screen.findByRole("button", { name: /review and confirm/i }));
    current = summary({ token: "tk2" });
    expect(await screen.findByText(/something changed meanwhile/i)).toBeInTheDocument();
    // The card stays open and now carries the new token.
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: /review and confirm/i }));
    await waitFor(() =>
      expect(postMock).toHaveBeenLastCalledWith("/personal-department/confirm", { token: "tk2" }),
    );
  });

  it("shows the refusal of a 422", async () => {
    postMock.mockRejectedValueOnce(
      new ApiError("x", 422, { detail: { error: "value_spec_not_supported" } }),
    );
    renderNotice();
    fireEvent.click(await screen.findByRole("button", { name: /review and confirm/i }));
    expect(await screen.findByText(/could not be confirmed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /review and confirm/i })).toBeInTheDocument();
  });

  it("shows an unknown skill status as given", async () => {
    current = summary({ skills: [{ name: "x", description: null, status: "weird" }] });
    renderNotice();
    expect(await screen.findByTestId("skill-status")).toHaveTextContent("weird");
  });

  it("invalidates the personal department, tool access and the admin list on success", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(qc, "invalidateQueries");
    renderNotice(qc);
    fireEvent.click(await screen.findByRole("button", { name: /review and confirm/i }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ["personal-department"] }));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["personal-departments"] });
    const call = spy.mock.calls
      .map((c) => c[0] as { queryKey: unknown[]; predicate?: (q: unknown) => boolean })
      .find((c) => c.queryKey[0] === "agents");
    expect(call?.predicate?.({ queryKey: ["agents", "a1", "tool-access"] })).toBe(true);
    expect(call?.predicate?.({ queryKey: ["agents", "a1", "runs"] })).toBe(false);
  });
});
