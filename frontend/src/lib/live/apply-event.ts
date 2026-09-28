import type { QueryClient } from "@tanstack/react-query";

import type { Agent, ActivityItem } from "@/lib/mock-data";
import type { RealtimeEvent } from "@/lib/live/types";
import type { RunComponentDTO } from "@/lib/hooks";

// Query keys that carry live-updatable data (resynced on reconnect).
export const liveQueryKeys: readonly unknown[][] = [
  ["agents"],
  ["activity"],
  ["approvals", "pending"],
  ["clarifications"],
  ["handoffs"],
  ["flow-runs"],
  ["supervision-interventions"],
  ["departments"],
];

export interface McpTestLogLine {
  step: string;
  message: string;
}

// Not in liveQueryKeys above: unlike those, this cache entry has no backing
// GET endpoint to resync from on reconnect -- it exists only while a "Test
// connection" log drawer is open, reset by the caller (McpTestLogDrawer)
// each time a new test starts.
export function mcpTestLogKey(connectionId: string): unknown[] {
  return ["mcp-test-log", connectionId];
}

type Patcher = (qc: QueryClient, data: Record<string, unknown>) => void;

const patchers: Record<string, Patcher> = {
  "run.status": (qc, d) => {
    const runId = d.run_id as string | undefined;
    if (runId) {
      qc.setQueryData(["run", runId], (prev: unknown) =>
        prev ? { ...(prev as object), state: d.state, phase: d.phase } : prev,
      );
    }
    // terminal states free the agent -> reflect in the agents list (mirrors the
    // old useRun poll's terminal-state agents invalidation)
    if (d.state === "done" || d.state === "failed" || d.state === "interrupted") {
      qc.invalidateQueries({ queryKey: ["agents"] });
    }
    // A run parking on a question is the only signal that a clarification
    // appeared — there is no `clarification.created` event. Narrowed to that one
    // state on purpose: `run.status` fires on every transition, and refetching
    // the workspace queue on all of them would be a request per step.
    if (d.state === "waiting_for_input") {
      qc.invalidateQueries({ queryKey: ["clarifications"] });
    }
  },
  // Incremental container stdout, fired by the three containerized runtimes
  // (opencode/codex/claude_code) from inside their poll loop -- see
  // plugins/*/runtime/runtime.py's tail_new_lines() call sites and
  // backend/src/oc8/realtime/emit.py's publish_run_output_delta. Appended,
  // never replacing what is already cached: a page load starts with no
  // transcript (this event is not replayed from history), so LiveLog only
  // ever shows what arrived while it was open.
  "run.output_delta": (qc, d) => {
    const runId = d.run_id as string | undefined;
    const chunk = d.chunk as string | undefined;
    if (!runId || !chunk) return;
    qc.setQueryData(["run", runId], (prev: unknown) =>
      prev
        ? {
            ...(prev as { transcript?: string[] }),
            transcript: [...((prev as { transcript?: string[] }).transcript ?? []), chunk],
          }
        : prev,
    );
  },
  // A structured card the agent chose to show instead of only prose --
  // see backend/src/oc8/agent/control_tools.py's render_component and
  // backend/src/oc8/agent/components.py's catalogue. Appended, same as
  // run.output_delta, and for the same reason: nothing here is replayed
  // from history on a fresh page load.
  "run.component_rendered": (qc, d) => {
    const runId = d.run_id as string | undefined;
    const componentKey = d.component_key as string | undefined;
    const props = d.props as Record<string, unknown> | undefined;
    if (!runId || !componentKey || !props) return;
    qc.setQueryData(["run", runId], (prev: unknown) =>
      prev
        ? {
            ...(prev as { components?: RunComponentDTO[] }),
            components: [
              ...((prev as { components?: RunComponentDTO[] }).components ?? []),
              { componentKey, props },
            ],
          }
        : prev,
    );
  },
  // One tool call the in-process (or isolated-container) engine just
  // executed -- see backend/src/oc8/realtime/emit.py's publish_run_tool_call
  // and its call site in agent/engine.py's step loop. Appended, same as
  // run.output_delta/run.component_rendered: the backend already persisted
  // this same entry via oc8.runtime.run_context.append_tool_call before
  // publishing, so a fresh page load sees it from the initial GET /runs/{id}
  // fetch too -- this event only spares an ALREADY-OPEN tab the wait for a
  // refetch, the same "spare a wait, not the only path to the data" role
  // run.status plays next to a plain poll.
  "run.tool_call": (qc, d) => {
    const runId = d.run_id as string | undefined;
    const call = d.call as Record<string, unknown> | undefined;
    if (!runId || !call) return;
    qc.setQueryData(["run", runId], (prev: unknown) =>
      prev
        ? {
            ...(prev as { toolCalls?: Record<string, unknown>[] }),
            toolCalls: [
              ...((prev as { toolCalls?: Record<string, unknown>[] }).toolCalls ?? []),
              call,
            ],
          }
        : prev,
    );
  },
  // One text fragment of the model's own answer as it streams in (Stage 2
  // token streaming) -- see backend/src/oc8/realtime/emit.py's
  // publish_run_token_delta, called from BOTH engines' streaming accumulator
  // callback (agent/engine.py's _live_token_delta and api/v1/internal_agent.
  // py's /step). Concatenated, not appended as a separate array element like
  // run.output_delta's stdout lines -- these fragments are words/tokens of
  // one continuous answer, not lines of a log.
  "run.token_delta": (qc, d) => {
    const runId = d.run_id as string | undefined;
    const text = d.text as string | undefined;
    if (!runId || !text) return;
    qc.setQueryData(["run", runId], (prev: unknown) =>
      prev
        ? {
            ...(prev as { liveAnswer?: string }),
            liveAnswer: ((prev as { liveAnswer?: string }).liveAnswer ?? "") + text,
          }
        : prev,
    );
  },
  // The agent's whole to-do list as of the last todo_write call (whole-list
  // replace, not appended like toolCalls/components above) -- see
  // backend/src/oc8/agent/control_tools.py's ControlOutcome.todos and its
  // publish call sites in engine.py/internal_agent.py.
  "run.todos_updated": (qc, d) => {
    const runId = d.run_id as string | undefined;
    const todos = d.todos as unknown[] | undefined;
    if (!runId || !todos) return;
    qc.setQueryData(["run", runId], (prev: unknown) =>
      prev ? { ...(prev as object), todos } : prev,
    );
  },
  "agent.status": (qc, d) => {
    const agentId = d.agent_id as string | undefined;
    const status = d.status as string | undefined;
    if (!agentId || !status) return;
    qc.setQueryData(["agents"], (prev: Agent[] | undefined) =>
      prev?.map((a) => (a.id === agentId ? { ...a, status: mapAgentStatus(status) } : a)),
    );
    qc.invalidateQueries({ queryKey: ["agents", agentId] });
  },
  "activity.logged": (qc) => {
    // Invalidate rather than prepend: the feed is now keyed by (agent, limit)
    // and paged on the server, so patching one cache entry by hand would leave
    // the others stale and could duplicate a row the next page also returns.
    qc.invalidateQueries({ queryKey: ["activity"] });
  },
  "approval.created": (qc) => qc.invalidateQueries({ queryKey: ["approvals", "pending"] }),
  "approval.decided": (qc) => qc.invalidateQueries({ queryKey: ["approvals"] }),
  "handoff.status": (qc) => qc.invalidateQueries({ queryKey: ["handoffs"] }),
  "flow_run.status": (qc) => qc.invalidateQueries({ queryKey: ["flow-runs"] }),
  "supervision.intervention": (qc) =>
    qc.invalidateQueries({ queryKey: ["supervision-interventions"] }),
  // One step of a "Test connection" run (spawn/handshake/list_tools/result)
  // -- see backend/src/oc8/realtime/emit.py's publish_mcp_test_log. Appended,
  // same shape as run.output_delta: nothing here is replayed from history,
  // so a drawer opened after the test finished only shows what arrives on
  // the NEXT test run.
  "mcp.test.log": (qc, d) => {
    const connectionId = d.connection_id as string | undefined;
    const step = d.step as string | undefined;
    const message = d.message as string | undefined;
    if (!connectionId || !step || !message) return;
    qc.setQueryData(mcpTestLogKey(connectionId), (prev: McpTestLogLine[] | undefined) => [
      ...(prev ?? []),
      { step, message },
    ]);
  },
};

// backend agent.status -> mock UI AgentStatus (running|warning|error|paused|waiting_for_task)
// Handles BOTH vocabularies: the WS "agent.status" event carries the raw
// backend column ("idle", "waiting_for_approval", ...), while the REST
// agent endpoints already pre-map through AGENT_STATUS_TO_UI
// (backend/src/oc8/api/v1/_serializers.py) and send "waiting_for_task"
// directly -- so an already-mapped value must pass through unchanged
// rather than fall into the "anything else" bucket below.
function mapAgentStatus(s: string): Agent["status"] {
  if (s === "running") return "running";
  if (s === "error") return "error";
  if (s === "idle" || s === "waiting_for_task") return "waiting_for_task";
  if (s === "paused" || s === "waiting_for_approval" || s === "pending_approval") return "paused";
  // stopped | anything else -> not a UI alert; keep as paused-ish "idle" look
  return "paused";
}

function mapActivityStatus(s: string): ActivityItem["status"] {
  if (s === "success" || s === "warning" || s === "error" || s === "info") {
    return s;
  }
  return "info";
}

export function applyEvent(qc: QueryClient, e: RealtimeEvent): void {
  patchers[e.type]?.(qc, e.data);
}
