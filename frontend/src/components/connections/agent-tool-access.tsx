import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useConfirm } from "@/hooks/use-confirm";
import {
  type AccessOption,
  type AgentToolAccess,
  type Integration,
  useAgentToolAccess,
  useAgentToolAccessOptions,
  useIntegrations,
  useResetAgentToolAccess,
  useSetAgentToolAccess,
} from "@/lib/connections";
import { useT } from "@/lib/i18n";
import { IntegrationIcon } from "./integration-icon";

type SetBody = {
  mode: "user" | "fixed";
  oauthConnectionId?: string;
  credentialId?: string;
  confirmShare?: boolean;
};

const optionValue = (o: AccessOption) => (o.kind === "user" ? "user" : `${o.kind}:${o.id}`);

const valueOf = (row: AgentToolAccess) =>
  row.mode === "default"
    ? "default"
    : row.mode === "user"
      ? "user"
      : row.oauthConnectionId
        ? `oauth:${row.oauthConnectionId}`
        : row.credentialId
          ? `credential:${row.credentialId}`
          : "missing";

/** The body that binds the agent to `option` (never for the user mode). */
const fixedBody = (option: AccessOption, confirmShare: boolean): SetBody => ({
  mode: "fixed",
  ...(option.kind === "oauth"
    ? { oauthConnectionId: option.id ?? undefined }
    : { credentialId: option.id ?? undefined }),
  ...(confirmShare ? { confirmShare: true } : {}),
});

/** The agent's "Zugang" setting: per enabled connectable tool, whose account
 *  the agent acts through. The server decides which accounts the caller may
 *  pick (other people's only for administrators) and refuses the rest. */
export function AgentToolAccessPanel({
  agentId,
  toolKeys,
  mayManage,
}: {
  agentId: string;
  toolKeys: string[];
  mayManage: boolean;
}) {
  const t = useT();
  const { data = [] } = useAgentToolAccess(agentId);
  const { data: integrations = [] } = useIntegrations();
  const rows = data.filter((row) => toolKeys.includes(row.toolKey));
  if (rows.length === 0) return null;
  return (
    <Panel className="mt-4 p-5">
      <h3 className="font-serif text-lg">{t("Access", "Zugang")}</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        {t(
          "For scheduled runs the account of the person who created the schedule is used.",
          "Bei geplanten Läufen wird das Konto der Person genutzt, die den Zeitplan angelegt hat.",
        )}
      </p>
      <div className="mt-4 space-y-4">
        {rows.map((row) => (
          <AccessRow
            key={row.toolKey}
            agentId={agentId}
            row={row}
            integration={integrations.find((i) => i.name === row.integration)}
            disabled={!mayManage}
          />
        ))}
      </div>
    </Panel>
  );
}

function AccessRow({
  agentId,
  row,
  integration,
  disabled,
}: {
  agentId: string;
  row: AgentToolAccess;
  integration: Integration | undefined;
  disabled: boolean;
}) {
  const t = useT();
  const { data: options = [] } = useAgentToolAccessOptions(agentId, row.toolKey, !disabled);
  const setAccess = useSetAgentToolAccess(agentId);
  const reset = useResetAgentToolAccess(agentId);
  const { confirm, ConfirmDialog } = useConfirm();
  // Another person's account: confirm inline first. Component state, so a
  // background refetch never drops the open warning.
  const [pending, setPending] = useState<AccessOption | null>(null);
  const busy = setAccess.isPending || reset.isPending;

  const save = async (run: () => Promise<unknown>) => {
    try {
      await run();
      toast.success(t("Access saved.", "Zugang gespeichert."));
    } catch {
      toast.error(
        t(
          "The access could not be saved. Please try again.",
          "Der Zugang konnte nicht gespeichert werden. Bitte versuchen Sie es erneut.",
        ),
      );
    }
  };
  const put = (body: SetBody) => save(() => setAccess.mutateAsync({ toolKey: row.toolKey, body }));

  const shareOwn = async (option: AccessOption) => {
    const ok = await confirm({
      title: t("Share your account with this agent?", "Ihr Konto für diesen Agenten freigeben?"),
      description: t(
        `When colleagues work with this agent, it uses your account ${option.label} for them.`,
        `Wenn Kolleginnen und Kollegen mit diesem Agenten arbeiten, nutzt er dafür Ihr Konto ${option.label}.`,
      ),
      confirmLabel: t("Share account", "Konto freigeben"),
      cancelLabel: t("Cancel", "Abbrechen"),
      destructive: false,
    });
    if (ok) await put(fixedBody(option, true));
  };

  const onChange = (value: string) => {
    setPending(null);
    if (value === "default") return void save(() => reset.mutateAsync(row.toolKey));
    const option = options.find((o) => optionValue(o) === value);
    if (!option) return;
    if (option.kind === "user") return void put({ mode: "user" });
    if (option.owner === "me") return void shareOwn(option);
    if (option.owner === "member") return setPending(option);
    void put(fixedBody(option, false));
  };

  const optionLabel = (o: AccessOption) => {
    if (o.kind === "user") return t("In the user's context", "Im Kontext des Nutzers");
    if (o.owner === "organisation")
      return t(`Organisation: ${o.label}`, `Organisation: ${o.label}`);
    if (o.owner === "me") return t(`Your account: ${o.label}`, `Ihr Konto: ${o.label}`);
    return t(`Account of ${o.ownerName}: ${o.label}`, `Konto von ${o.ownerName}: ${o.label}`);
  };
  const defaultLabel =
    row.defaultMode === "user"
      ? t("Default: in the user's context", "Standard: im Kontext des Nutzers")
      : t("Default: the organisation's access", "Standard: Zugang der Organisation");

  const current = valueOf(row);
  // The bound account as the caller may pick it: only then may they confirm
  // it again (its owner, or an administrator, who sees everybody's).
  const boundOption = options.find((o) => o.kind !== "user" && optionValue(o) === current);

  return (
    <div className="space-y-2">
      <label className="flex flex-wrap items-center justify-between gap-3 text-sm">
        <span className="flex items-center gap-2 font-medium">
          {integration && <IntegrationIcon integration={integration} size={20} />}
          {row.label}
        </span>
        <select
          aria-label={t(`Access for ${row.label}`, `Zugang für ${row.label}`)}
          disabled={disabled || busy}
          value={current}
          onChange={(event) => onChange(event.target.value)}
          className="min-h-11 max-w-full rounded-md border border-border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60"
        >
          <option value="default">{defaultLabel}</option>
          {row.mode === "fixed" && row.targetMissing && (
            <option value="missing">
              {t("Removed account, please choose again", "Entferntes Konto, bitte neu wählen")}
            </option>
          )}
          {row.mode === "fixed" && !row.targetMissing && !boundOption && (
            <option value={current}>
              {row.targetOwnerName
                ? t(
                    `Account of ${row.targetOwnerName}: ${row.targetLabel}`,
                    `Konto von ${row.targetOwnerName}: ${row.targetLabel}`,
                  )
                : row.targetLabel}
            </option>
          )}
          {options.map((o) => (
            <option key={optionValue(o)} value={optionValue(o)}>
              {optionLabel(o)}
            </option>
          ))}
        </select>
      </label>
      {row.needsConfirmation && !pending && (
        <div
          role="alert"
          className="rounded-md border border-orange-500/50 bg-orange-500/10 p-3 text-sm"
        >
          <p>
            {row.confirmationReason === "agent_changed"
              ? t(
                  "The agent changed since this access was confirmed. Runs that need it fail until it is confirmed again.",
                  "Der Agent hat sich geändert, seit dieser Zugang bestätigt wurde. Läufe, die ihn brauchen, schlagen fehl, bis er erneut bestätigt ist.",
                )
              : t(
                  "This access is not confirmed yet. Runs that need it fail until it is confirmed.",
                  "Dieser Zugang ist noch nicht bestätigt. Läufe, die ihn brauchen, schlagen fehl, bis er bestätigt ist.",
                )}
          </p>
          {boundOption && !disabled ? (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                // Your own account: re-tell what sharing means before it is
                // shared again. A colleague's (administrators only): direct.
                void (boundOption.owner === "me"
                  ? shareOwn(boundOption)
                  : put(fixedBody(boundOption, false)))
              }
              className="mt-2 min-h-11 rounded-md border border-border bg-background px-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60"
            >
              {t("Confirm", "Bestätigen")}
            </button>
          ) : (
            <p className="mt-1 text-muted-foreground">
              {disabled
                ? t(
                    "You cannot edit this agent. Please ask an administrator or someone who may edit the agent.",
                    "Sie können diesen Agenten nicht bearbeiten. Bitte wenden Sie sich an eine Administratorin bzw. einen Administrator oder an jemanden, der den Agenten bearbeiten darf.",
                  )
                : t(
                    "The account's owner or an administrator can confirm it.",
                    "Bestätigen können der Besitzer des Kontos oder eine Administratorin bzw. ein Administrator.",
                  )}
            </p>
          )}
        </div>
      )}
      {pending && (
        <div
          role="alert"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
        >
          <p>
            {t(
              `Everybody working with this agent then acts with the account of ${pending.ownerName}.`,
              `Alle, die mit diesem Agenten arbeiten, handeln dann mit dem Konto von ${pending.ownerName}.`,
            )}
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              className="min-h-11 rounded-md border border-border px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              onClick={() => {
                const option = pending;
                setPending(null);
                void put(fixedBody(option, false));
              }}
            >
              {t("Set anyway", "Trotzdem festlegen")}
            </button>
            <button
              type="button"
              className="min-h-11 rounded-md px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              onClick={() => setPending(null)}
            >
              {t("Cancel", "Abbrechen")}
            </button>
          </div>
        </div>
      )}
      {ConfirmDialog}
    </div>
  );
}
