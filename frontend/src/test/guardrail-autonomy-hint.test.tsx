import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";
import type { McpConnection } from "@/lib/hooks";

// E2E F2: a preset meant for autonomy (outward tools allowed, no approval
// asked) still asked before every send, because the agent itself was not
// autonomous with the outward tier granted -- and nothing said so.

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useConnectionToolNames: () => ({
      data: { names: ["mail_get", "mail_send"], read: ["mail_get"] },
    }),
  };
});
vi.mock("@/lib/hooks-agent-detail", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-agent-detail")>();
  return { ...actual, useAgentToolUsage: () => ({ data: [] }) };
});
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Link: ({ children, hash }: { children: ReactNode; hash?: string }) => (
      <a href={`#${hash ?? ""}`}>{children}</a>
    ),
  };
});

import { ToolGuardrailEditorPanel } from "@/components/tool-guardrail-editor-panel";

const email = {
  name: "email",
  guardrailPresets: [],
  guardrailLibrary: null,
  hasValueSpec: false,
  outwardTools: ["mail_send"],
} as unknown as McpConnection;

const sendsUnasked: GuardrailValue = {
  read: true,
  modify: true,
  approvalActions: [],
  approvalEur: null,
  only: [],
  conditions: [],
  limits: [{ tools: ["mail_send"], max: 50, per: "day" }],
};

function renderPanel(
  value: GuardrailValue,
  autonomy: "default" | "autonomous",
  grants: string[],
  ceiling: GuardrailValue | null = null,
) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ToolGuardrailEditorPanel
        toolKey="email"
        connection={email}
        ceiling={ceiling}
        value={value}
        onChange={vi.fn()}
        onCancel={vi.fn()}
        onSave={vi.fn()}
        saving={false}
        agentId="a1"
        agentAutonomy={{ autonomy, autonomyGrants: grants }}
      />
    </QueryClientProvider>,
  );
}

describe("autonomy hint on the guardrail editor", () => {
  it("points to the autonomy setting when outward tools are allowed unasked", () => {
    renderPanel(sendsUnasked, "default", []);
    expect(screen.getByText(/still ask before each one/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /autonomy setting/i })).toHaveAttribute(
      "href",
      "#agent-autonomy",
    );
  });

  it("stays quiet once the agent is autonomous with the outward tier", () => {
    renderPanel(sendsUnasked, "autonomous", ["outward"]);
    expect(screen.queryByText(/still ask before each one/i)).not.toBeInTheDocument();
  });

  it("stays quiet when the department asks before sending anyway", () => {
    // Review of F2: the department frame's approval actions hold too, so the
    // agent asks there whatever its own guardrails say.
    renderPanel(sendsUnasked, "default", [], {
      ...sendsUnasked,
      approvalActions: ["mail_send"],
    });
    expect(screen.queryByText(/still ask before each one/i)).not.toBeInTheDocument();
  });

  it("only says it may ask when a condition decides", () => {
    renderPanel(
      {
        ...sendsUnasked,
        conditions: [
          { attribute: "recipient_domain", op: "not_in", value: ["acme.test"], approval: true },
        ] as unknown as GuardrailValue["conditions"],
      },
      "default",
      [],
    );
    expect(screen.getByText(/may still ask before each one/i)).toBeInTheDocument();
  });

  it("stays quiet when the guardrails ask before sending anyway", () => {
    renderPanel({ ...sendsUnasked, approvalActions: ["mail_send"] }, "default", []);
    expect(screen.queryByText(/still ask before each one/i)).not.toBeInTheDocument();
  });
});
