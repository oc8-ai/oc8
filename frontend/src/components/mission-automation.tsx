import { Check, Clock3, Play, Save, ShieldAlert, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import {
  useAgentTriggers,
  useAutomationEventCatalogue,
  useCreateAgentTrigger,
  useUpdateAgentAutomation,
} from "@/lib/hooks";

type Autonomy = "review" | "guided" | "independent";

export function MissionAutomation({
  agentId,
  agentName,
  initialMission,
  readOnly = false,
}: {
  agentId: string;
  agentName: string;
  initialMission: string;
  readOnly?: boolean;
}) {
  const [mission, setMission] = useState(initialMission);
  const [eventKey, setEventKey] = useState("");
  const [taskText, setTaskText] = useState("");
  const [autonomy, setAutonomy] = useState<Autonomy>("guided");
  const [escalation, setEscalation] = useState(
    "Ask a person when the outcome is unclear or an external action is needed.",
  );
  const { data: triggers = [] } = useAgentTriggers(agentId);
  const { data: events = [] } = useAutomationEventCatalogue();
  const updateAutomation = useUpdateAgentAutomation();
  const createTrigger = useCreateAgentTrigger();

  useEffect(() => setMission(initialMission), [initialMission]);
  const selectedEvent = useMemo(
    () => events.find((event) => `${event.source}:${event.type}` === eventKey),
    [eventKey, events],
  );

  const saveMission = () => {
    updateAutomation.mutate(
      { agentId, mission: mission.trim(), triggerIds: triggers.map((trigger) => trigger.id) },
      {
        onSuccess: () => toast.success("Mission saved"),
        onError: (error) => toast.error(error.message),
      },
    );
  };

  const addEventTrigger = () => {
    if (!selectedEvent || !taskText.trim()) return;
    createTrigger.mutate(
      {
        agentId,
        kind: "event",
        taskText: taskText.trim(),
        eventSource: selectedEvent.source,
        eventType: selectedEvent.type,
      },
      {
        onSuccess: () => {
          setTaskText("");
          toast.success("Start condition added");
        },
        onError: (error) => toast.error(error.message),
      },
    );
  };

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel className="p-5">
        <div className="flex items-start gap-3">
          <span className="grid h-8 w-8 place-items-center rounded-full bg-primary/15 text-primary">
            1
          </span>
          <div>
            <h3 className="font-medium">Mission</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Describe the outcome {agentName} should own in everyday language.
            </p>
          </div>
        </div>
        <textarea
          aria-label="Mission"
          value={mission}
          disabled={readOnly}
          onChange={(event) => setMission(event.target.value)}
          rows={5}
          className="mt-4 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:opacity-60"
        />
        <button
          type="button"
          disabled={readOnly || !mission.trim() || updateAutomation.isPending}
          onClick={saveMission}
          className="mt-3 inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          <Save className="h-4 w-4" /> Save mission
        </button>
      </Panel>

      <Panel className="p-5">
        <div className="flex items-start gap-3">
          <span className="grid h-8 w-8 place-items-center rounded-full bg-primary/15 text-primary">
            2
          </span>
          <div>
            <h3 className="font-medium">Start condition</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Choose what should start this work. Connection details and credentials stay private.
            </p>
          </div>
        </div>
        <select
          aria-label="Start condition"
          disabled={readOnly}
          value={eventKey}
          onChange={(event) => setEventKey(event.target.value)}
          className="mt-4 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm disabled:opacity-60"
        >
          <option value="">Select an event</option>
          {events.map((event) => (
            <option key={`${event.source}:${event.type}`} value={`${event.source}:${event.type}`}>
              {event.label}
            </option>
          ))}
        </select>
        {selectedEvent?.description && (
          <p className="mt-2 text-xs text-muted-foreground">{selectedEvent.description}</p>
        )}
        <input
          aria-label="Work when this starts"
          disabled={readOnly}
          value={taskText}
          onChange={(event) => setTaskText(event.target.value)}
          placeholder="What should the agent do?"
          className="mt-3 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm disabled:opacity-60"
        />
        <button
          type="button"
          disabled={readOnly || !selectedEvent || !taskText.trim() || createTrigger.isPending}
          onClick={addEventTrigger}
          className="mt-3 inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm disabled:opacity-50"
        >
          <Zap className="h-4 w-4" /> Add start condition
        </button>
      </Panel>

      <Panel className="p-5">
        <div className="flex items-start gap-3">
          <span className="grid h-8 w-8 place-items-center rounded-full bg-primary/15 text-primary">
            3
          </span>
          <div>
            <h3 className="font-medium">Tools & knowledge</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Use the Access and Knowledge tabs to choose approved tools and information sources.
            </p>
          </div>
        </div>
        <div className="mt-4 rounded-md border border-border bg-background/30 p-3 text-sm text-muted-foreground">
          Only approved provider labels and opaque references are used here—never credentials or
          connection settings.
        </div>
      </Panel>

      <Panel className="p-5">
        <div className="flex items-start gap-3">
          <span className="grid h-8 w-8 place-items-center rounded-full bg-primary/15 text-primary">
            4
          </span>
          <div>
            <h3 className="font-medium">Autonomy & escalation</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Set how much the agent can decide before involving a person.
            </p>
          </div>
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-3">
          {(["review", "guided", "independent"] as Autonomy[]).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setAutonomy(option)}
              disabled={readOnly}
              className={`rounded-md border px-3 py-2 text-sm capitalize disabled:opacity-60 ${autonomy === option ? "border-primary bg-primary/10 text-primary" : "border-border"}`}
            >
              {option}
            </button>
          ))}
        </div>
        <label className="mt-4 block text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Escalate when
        </label>
        <textarea
          aria-label="Escalate when"
          rows={3}
          value={escalation}
          onChange={(event) => setEscalation(event.target.value)}
          disabled={readOnly}
          className="mt-2 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm disabled:opacity-60"
        />
      </Panel>

      <Panel className="p-5 lg:col-span-2">
        <div className="flex items-center gap-2">
          <Clock3 className="h-4 w-4 text-primary" />
          <h3 className="font-medium">Current start conditions</h3>
        </div>
        {triggers.length === 0 ? (
          <p className="mt-3 text-sm text-muted-foreground">
            No automated start conditions yet. This agent can still be run manually.
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {triggers.map((trigger) => (
              <li
                key={trigger.id}
                className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm"
              >
                <Play className="h-3.5 w-3.5 text-primary" />
                {trigger.kind === "event"
                  ? `${trigger.eventSource} · ${trigger.eventType}`
                  : trigger.cronExpression}
                <span className="text-muted-foreground">— {trigger.taskText}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-4 flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-sm">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-status-warning-text" />
          Critical actions should always be sent to approval; automation never bypasses access
          rules.
        </div>
      </Panel>
    </div>
  );
}
