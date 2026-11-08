import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { GuardrailPresetPicker, type GuardrailValue } from "@/components/guardrail-preset-picker";
import { useT } from "@/lib/i18n";
import { useDepartmentTools, useMcpConnections, useSetDepartmentTools } from "@/lib/hooks";

/** Presents the guardrails for a just-connected tool -- the plugin's named
 * presets if it ships any, plus free controls otherwise or via "Configure
 * myself" -- and saves the result into the department's tool "frame". The
 * question this step asks ("how far may this tool go on its own") applies
 * the same whether the connection is a sales CRM, a helpdesk, or a knowledge
 * source; it does not presume what kind of agent is asking.
 *
 * `PUT /departments/{id}/tools` REPLACES the entire `tools` dict server-side
 * -- it does not merge/patch. So `save` below spreads the existing
 * `existing?.tools` into the outgoing body before adding the new entry;
 * dropping that spread would silently delete every other tool already
 * granted to this department. */
export function GuardrailsStep({
  departmentId,
  connectionId,
  onDone,
}: {
  departmentId: string;
  connectionId: string;
  onDone: () => void;
}) {
  const t = useT();
  const {
    data: existing,
    isLoading: toolsLoading,
    isError: toolsErrored,
    error: toolsError,
    refetch: refetchTools,
  } = useDepartmentTools(departmentId);
  const { data: connections = [] } = useMcpConnections();
  const setTools = useSetDepartmentTools(departmentId);
  const [value, setValue] = useState<GuardrailValue>({
    read: true,
    modify: false,
    approvalActions: [],
    approvalEur: null,
    only: [],
    conditions: [],
  });

  const connection = connections.find((c) => c.id === connectionId);
  const presets = connection?.guardrailPresets ?? [];
  const guardrailLibrary = connection?.guardrailLibrary ?? null;
  const hasValueSpec = connection?.hasValueSpec ?? false;

  // Gate the whole panel -- and, critically, the Continue button -- on the
  // existing tool frame having actually loaded, successfully. Rendering the
  // button before `existing` resolves (still loading) or after the fetch
  // fails (isError) would let `save` run with `existing?.tools` still
  // undefined, silently dropping every already-granted tool from the PUT
  // body -- the exact regression this step exists to prevent. `isLoading` is
  // `isPending && isFetching`, which flips to `false` on a failed fetch too
  // (after react-query's retries are exhausted), so both cases must be
  // checked; a loading-only guard would leave the error path unguarded.
  if (toolsLoading) {
    return (
      <Panel className="space-y-4 p-6">
        <p className="text-sm text-muted-foreground">{t("Loading…", "Wird geladen …")}</p>
      </Panel>
    );
  }

  if (toolsErrored) {
    return (
      <Panel className="space-y-4 p-6">
        <p className="text-sm text-destructive">
          {t(
            "Could not load this department's existing tools, so it isn't safe to continue -- saving now could delete tools it already has.",
            "Die vorhandenen Tools dieser Abteilung konnten nicht geladen werden, daher ist ein Fortfahren nicht sicher -- ein Speichern jetzt könnte bereits vorhandene Tools löschen.",
          )}
        </p>
        {toolsError instanceof Error && (
          <p className="text-xs text-muted-foreground">{toolsError.message}</p>
        )}
        <button
          type="button"
          onClick={() => refetchTools()}
          className="w-full rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground transition hover:bg-muted/40"
        >
          {t("Try again", "Erneut versuchen")}
        </button>
      </Panel>
    );
  }

  // The frame is keyed by the connection's NAME, not its database id (see
  // `save` below) -- so `save` needs the resolved `connection`, not just the
  // `connectionId` prop. If connections haven't loaded yet, or `connectionId`
  // doesn't match anything in the list, `connection` is `undefined`: same
  // discipline as the `toolsErrored` guard above -- block Continue entirely
  // rather than fall back to writing the UUID (reproduces the very bug this
  // guard exists to prevent) or an empty-string key (worse).
  if (!connection) {
    return (
      <Panel className="space-y-4 p-6">
        <p className="text-sm text-destructive">
          {t(
            "Could not resolve this connection, so it isn't safe to continue -- saving now could write guardrails under the wrong key.",
            "Diese Verbindung konnte nicht aufgelöst werden, daher ist ein Fortfahren nicht sicher -- ein Speichern jetzt könnte Guardrails unter dem falschen Schlüssel speichern.",
          )}
        </p>
      </Panel>
    );
  }

  const save = async () => {
    const merged = {
      ...(existing?.tools ?? {}),
      [connection.name]: {
        enabled: true,
        read: value.read,
        modify: value.modify,
        approval_eur: value.approvalEur,
        approval_actions: value.approvalActions,
        // Not decoration -- see `GuardrailValue`/`GuardrailPreset` in
        // `@/components/guardrail-preset-picker`. Dropping this here would
        // silently turn a preset like "Autonomous with a limit" into
        // read+write+send above a euro threshold with EVERY tool reachable,
        // including `delete_record`, which carries no amount and so can
        // never meet the threshold -- the unattended-deletion configuration,
        // under a name promising a limit.
        only: value.only,
      },
    };
    try {
      await setTools.mutateAsync(merged);
      toast.success(t("Guardrails saved", "Guardrails gespeichert"));
      onDone();
    } catch (err) {
      toast.error(t("Could not save guardrails", "Guardrails konnten nicht gespeichert werden"), {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <Panel className="space-y-4 p-6">
      <p className="text-sm text-muted-foreground">
        {t(
          `How far may ${connection?.name ?? "this tool"} go on its own -- a lookup, a change, a message it sends without asking first?`,
          `Wie weit darf ${connection?.name ?? "dieses Tool"} selbstständig gehen -- eine Abfrage, eine Änderung, eine Nachricht, die es ohne Rückfrage verschickt?`,
        )}
      </p>
      <GuardrailPresetPicker
        presets={presets}
        guardrailLibrary={guardrailLibrary}
        hasValueSpec={hasValueSpec}
        value={value}
        onChange={setValue}
      />
      <button
        onClick={save}
        disabled={setTools.isPending}
        className="w-full rounded-md bg-brand-gradient px-4 py-2 text-sm font-medium text-brand-gradient-foreground transition hover:brightness-110 disabled:opacity-60"
      >
        {setTools.isPending ? t("Saving…", "Wird gespeichert …") : t("Continue", "Weiter")}
      </button>
    </Panel>
  );
}
