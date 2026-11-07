import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MemberGuardrail, MyGuardrails, MyToolGuardrail } from "@/lib/hooks-member-guardrails";

const POLICY = {
  enabled: true,
  read: true,
  modify: true,
  approvalEur: null,
  approvalActions: [],
  only: null,
  connectionId: null,
  conditions: [],
};

function tool(over: Partial<MyToolGuardrail> = {}): MyToolGuardrail {
  return {
    toolKey: "gmail",
    connectionName: "gmail",
    base: POLICY,
    baseLevel: "allowed",
    mine: null,
    level: "allowed",
    hasLimits: false,
    source: "agent",
    ...over,
  };
}

const m = vi.hoisted(() => ({
  data: undefined as MyGuardrails | undefined,
  loading: false,
  set: vi.fn(),
  reset: vi.fn(),
  members: [] as MemberGuardrail[],
  lang: "en" as "en" | "de",
}));

vi.mock("@/lib/hooks-member-guardrails", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-member-guardrails")>();
  return {
    ...actual,
    useMyGuardrails: () => ({ data: m.data, isLoading: m.loading, isError: false }),
    useSetMyGuardrail: () => ({ mutateAsync: m.set, isPending: false }),
    useResetMyGuardrail: () => ({ mutateAsync: m.reset, isPending: false }),
    useMemberGuardrails: () => ({ data: m.members, isLoading: false }),
  };
});

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useConnectionToolNames: () => ({ data: { names: [] } }),
    useMcpLogins: () => ({ data: [] }),
  };
});

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return {
    ...actual,
    useT: () => (en: string, de?: string) => (m.lang === "de" ? (de ?? en) : en),
  };
});

import {
  MemberGuardrailsInsight,
  MemberGuardrailsPanel,
  YourGuardrailsButton,
} from "@/components/member-guardrails-sheet";

function data(over: Partial<MyGuardrails> = {}): MyGuardrails {
  return {
    agentId: "a1",
    agentName: "Sales agent",
    summary: [{ code: "may_modify", params: { tools: ["gmail"] } }],
    tools: [tool()],
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  m.data = data();
  m.loading = false;
  m.members = [];
  m.lang = "en";
  m.set.mockResolvedValue(data());
});

describe("YourGuardrailsButton", () => {
  it("is a quiet header entry with a count when the member has rules", async () => {
    m.data = data({
      tools: [
        tool({ mine: { ...POLICY, modify: false }, level: "read_only", source: "member" }),
        tool({ toolKey: "calendar", connectionName: "calendar" }),
      ],
    });
    render(<YourGuardrailsButton agentId="a1" agentName="Sales agent" />);
    const entry = screen.getByRole("button", { name: /Your guardrails/ });
    expect(within(entry).getByText("1")).toBeInTheDocument();
    fireEvent.click(entry);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Your guardrails · Sales agent")).toBeInTheDocument();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(entry).toHaveFocus());
  });

  it("shows no count without rules of the member's own", () => {
    render(<YourGuardrailsButton agentId="a1" agentName="Sales agent" />);
    const entry = screen.getByRole("button", { name: /Your guardrails/ });
    expect(entry.textContent?.trim()).toBe("Your guardrails");
  });

  it("reads German", () => {
    m.lang = "de";
    render(<YourGuardrailsButton agentId="a1" agentName="Vertriebsagent" />);
    expect(screen.getByRole("button", { name: /Deine Guardrails/ })).toBeInTheDocument();
  });
});

describe("MemberGuardrailsPanel", () => {
  it("opens with what the agent may do for me", () => {
    render(<MemberGuardrailsPanel agentId="a1" />);
    expect(screen.getByText("The sales agent may change things in gmail.")).toBeInTheDocument();
  });

  it("says the same in German", () => {
    m.lang = "de";
    m.data = data({ agentName: "Vertriebsagent" });
    render(<MemberGuardrailsPanel agentId="a1" />);
    expect(
      screen.getByText("Der Vertriebsagent darf in gmail Änderungen vornehmen."),
    ).toBeInTheDocument();
  });

  it("previews what it read and saves only on Confirm", async () => {
    render(<MemberGuardrailsPanel agentId="a1" />);
    const field = screen.getByLabelText("What should never happen without you?");
    fireEvent.change(field, { target: { value: "Never send a mail without me" } });
    fireEvent.submit(field.closest("form")!);
    expect(await screen.findByText("Here's how I read it:")).toBeInTheDocument();
    expect(screen.getByText("Gmail → Ask me first")).toBeInTheDocument();
    expect(m.set).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(m.set).toHaveBeenCalledWith({ toolKey: "gmail", level: "ask_first" }),
    );
  });

  it("says so, with two examples, when it can't match a tool", async () => {
    render(<MemberGuardrailsPanel agentId="a1" />);
    const field = screen.getByLabelText("What should never happen without you?");
    fireEvent.change(field, { target: { value: "never order pizza" } });
    fireEvent.submit(field.closest("form")!);
    expect(await screen.findByText("I couldn't match that to a tool.")).toBeInTheDocument();
    expect(screen.getAllByTestId("guardrail-example")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
    expect(m.set).not.toHaveBeenCalled();
  });

  it("says it in German when it can't match a tool", async () => {
    m.lang = "de";
    render(<MemberGuardrailsPanel agentId="a1" />);
    const field = screen.getByLabelText("Was soll nie ohne dich passieren?");
    fireEvent.change(field, { target: { value: "nie Pizza bestellen" } });
    fireEvent.submit(field.closest("form")!);
    expect(await screen.findByText("Das konnte ich keinem Werkzeug zuordnen.")).toBeInTheDocument();
  });

  it("without rules of my own, invites one and puts focus in the field", () => {
    render(<MemberGuardrailsPanel agentId="a1" />);
    expect(
      screen.getByText("No guardrails of your own yet. Say what should never happen without you."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("What should never happen without you?")).toHaveFocus();
  });

  it("loads as skeleton rows, not a spinner", () => {
    m.data = undefined;
    m.loading = true;
    const { container } = render(<MemberGuardrailsPanel agentId="a1" />);
    expect(screen.getAllByTestId("guardrail-skeleton").length).toBeGreaterThan(0);
    expect(container.querySelector(".animate-spin")).toBeNull();
  });

  it("shows the source as 'set by you' and the backend's level for my own row", () => {
    m.data = data({
      tools: [
        tool({
          mine: { ...POLICY, approvalActions: ["modify"] },
          level: "ask_first",
          source: "member",
        }),
      ],
    });
    render(<MemberGuardrailsPanel agentId="a1" />);
    expect(screen.getByText("set by you")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /gmail.*Ask me first/ })).toBeInTheDocument();
  });

  it("a failed level save snaps the row back and says so under it", async () => {
    m.set.mockRejectedValue(new Error("boom"));
    render(<MemberGuardrailsPanel agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: /gmail.*Allowed/ }));
    const radio = await screen.findByRole("radio", { name: /Read only/ });
    await act(async () => {
      fireEvent.click(radio);
    });
    await waitFor(() =>
      expect(m.set).toHaveBeenCalledWith({ toolKey: "gmail", level: "read_only" }),
    );
    expect(
      await screen.findByText("Couldn't save the new level, so it's back to what it was."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /gmail.*Allowed/ })).toBeInTheDocument();
  });

  it("a failed Reset keeps my rule and says so in one sentence under that row", async () => {
    m.reset.mockRejectedValue(new Error("boom"));
    m.data = data({
      tools: [
        tool({ mine: { ...POLICY, modify: false }, level: "read_only", source: "member" }),
        tool({ toolKey: "calendar", connectionName: "calendar" }),
      ],
    });
    render(<MemberGuardrailsPanel agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(m.reset).toHaveBeenCalledWith({ toolKey: "gmail" }));
    const sentence = await screen.findByText(/Couldn't reset this rule/);
    expect(sentence.closest("tr")).toHaveTextContent("gmail");
    expect(sentence.closest("tr")).not.toHaveTextContent("calendar");
    expect(screen.getAllByText(/Couldn't reset this rule/)).toHaveLength(1);
    expect(screen.getByRole("button", { name: /gmail.*Read only/ })).toBeInTheDocument();
  });

  it("a successful Reset clears an earlier failure", async () => {
    m.reset.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce(data());
    m.data = data({
      tools: [tool({ mine: { ...POLICY, modify: false }, level: "read_only", source: "member" })],
    });
    render(<MemberGuardrailsPanel agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await screen.findByText(/Couldn't reset this rule/);
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(screen.queryByText(/Couldn't reset this rule/)).toBeNull());
  });

  it("greys out levels the agent's own guardrail rules out", async () => {
    m.data = data({ tools: [tool({ baseLevel: "read_only", level: "read_only" })] });
    render(<MemberGuardrailsPanel agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: /gmail.*Read only/ }));
    const allowed = await screen.findByRole("radio", { name: /Allowed/ });
    expect(allowed).toHaveAttribute("aria-disabled", "true");
    expect(allowed).toHaveTextContent(/set by agent/);
  });
});

describe("MemberGuardrailsInsight", () => {
  it("counts members with their own guardrails and lists them read-only", () => {
    m.members = [
      {
        memberId: "m1",
        memberName: "Anna",
        tools: { gmail: { ...POLICY, modify: false } },
      },
      {
        memberId: "m2",
        memberName: "Ben",
        tools: { gmail: { ...POLICY, approvalActions: ["modify"] } },
      },
    ];
    render(<MemberGuardrailsInsight agentId="a1" />);
    fireEvent.click(screen.getByRole("button", { name: "2 members have their own guardrails" }));
    expect(screen.getByText("Anna")).toBeInTheDocument();
    expect(screen.getByText("gmail: Read only")).toBeInTheDocument();
    expect(screen.getByText("gmail: Asks them first")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Ask me first|Read only/ })).toBeNull();
  });

  it("stays quiet when no member has any", () => {
    const { container } = render(<MemberGuardrailsInsight agentId="a1" />);
    expect(container).toBeEmptyDOMElement();
  });
});
