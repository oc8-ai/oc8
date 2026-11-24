import { Link } from "@tanstack/react-router";
import { Loader2, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { FrameGuardrailsEditor } from "@/components/frame-guardrails-editor";
import { Switch } from "@/components/ui/switch";
import { useMay } from "@/lib/governance-hooks";
import {
  useFollowPersonalDefault,
  usePersonalDefaultBudget,
  usePersonalDefaultFrame,
  usePersonalDepartments,
  usePersonalDepartmentsSetting,
  useSetPersonalDefaultBudget,
  useSetPersonalDefaultFrame,
  useUpdatePersonalDepartmentsSetting,
  type PersonalDepartmentAdminDTO,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";

const MIN_AGENTS = 1;
const MAX_AGENTS = 1000;

const INPUT_CLASS =
  "mt-1 block w-32 rounded-md border border-border bg-background/40 px-2 py-1 text-sm text-foreground disabled:opacity-60";
const SAVE_CLASS =
  "mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-40";

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** Settings: the personal-departments switch, their default frame and budget,
 *  and the list of existing personal departments (metadata only). */
export function PersonalDepartmentsAdminPanel() {
  const t = useT();
  const may = useMay();
  const canViewSettings = may("settings:view") || may("settings:manage");
  const canViewBudget = may("budget:view") || may("budget:manage");
  const canManageDepartments = may("department:manage");
  const canFollow = canManageDepartments && may("budget:manage");
  if (!canViewSettings && !canViewBudget && !canManageDepartments) return null;

  return (
    <Panel id="personal-departments" className="p-5 md:col-span-2">
      <h3 className="font-serif text-lg">{t("Personal departments", "Persönliche Abteilungen")}</h3>
      <p className="mt-1 text-[11px] text-muted-foreground">
        {t(
          "Each member can get a private department of their own. Admins see only who owns one and how many agents it holds, never its content.",
          "Jedes Mitglied kann eine eigene, private Abteilung bekommen. Admins sehen nur, wem sie gehört und wie viele Agenten darin sind, nie den Inhalt.",
        )}
      </p>
      {canViewSettings && <SwitchSection canManage={may("settings:manage")} />}
      {canViewBudget && <BudgetSection canManage={may("budget:manage")} />}
      {canManageDepartments && <FrameSection />}
      {canManageDepartments && <PersonalList canFollow={canFollow} />}
    </Panel>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h4 className="mt-5 border-t border-border/60 pt-4 text-sm font-medium">{children}</h4>;
}

function SwitchSection({ canManage }: { canManage: boolean }) {
  const t = useT();
  const { data } = usePersonalDepartmentsSetting();
  const update = useUpdatePersonalDepartmentsSetting();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Reset the draft only when the server's cap itself changed: a refetch after
  // toggling the switch must not discard an unsaved edit.
  const synced = useRef<number | null>(null);
  useEffect(() => {
    if (data && data.maxAgents !== synced.current) {
      synced.current = data.maxAgents;
      setDraft(String(data.maxAgents));
    }
  }, [data]);

  const value = Number(draft);
  const valid = Number.isInteger(value) && value >= MIN_AGENTS && value <= MAX_AGENTS;

  const save = async (body: { enabled: boolean; maxAgents: number }) => {
    setError(null);
    try {
      await update.mutateAsync(body);
      toast.success(t("Personal departments updated", "Persönliche Abteilungen aktualisiert"));
    } catch (err) {
      setError(
        errorText(
          err,
          t(
            "Could not update the personal departments setting.",
            "Die Einstellung konnte nicht aktualisiert werden.",
          ),
        ),
      );
    }
  };

  return (
    <section aria-label={t("Switch", "Schalter")} className="mt-4">
      <div className="flex items-center gap-3">
        <Switch
          aria-label={t("Personal departments", "Persönliche Abteilungen")}
          checked={data?.enabled ?? false}
          disabled={!canManage || !data || update.isPending}
          onCheckedChange={(enabled) => {
            if (data) void save({ enabled, maxAgents: data.maxAgents });
          }}
        />
        <span className="text-sm">
          {t(
            "Let members have a personal department",
            "Mitgliedern eine persönliche Abteilung erlauben",
          )}
        </span>
      </div>
      <label className="mt-3 block text-xs text-muted-foreground">
        {t("Max personal agents per person", "Max. persönliche Agenten pro Person")}
        <input
          type="number"
          min={MIN_AGENTS}
          max={MAX_AGENTS}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={!canManage}
          className={INPUT_CLASS}
        />
      </label>
      <p className="mt-1 text-[11px] text-muted-foreground">
        {t(
          "Between 1 and 1000. Lowering it keeps existing agents but blocks new ones above the limit.",
          "Zwischen 1 und 1000. Ein niedrigerer Wert behält bestehende Agenten, blockiert aber neue über dem Limit.",
        )}
      </p>
      {canManage && (
        <button
          type="button"
          onClick={() => data && save({ enabled: data.enabled, maxAgents: value })}
          disabled={!valid || update.isPending || value === data?.maxAgents}
          className={SAVE_CLASS}
        >
          {update.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Save className="h-3 w-3" />
          )}
          {t("Save", "Speichern")}
        </button>
      )}
      {error && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}

function toDraft(n: number | null | undefined): string {
  return n == null ? "" : String(n);
}

/** Empty means "no limit" (null); anything else must be a whole number >= 0. */
function parseLimit(raw: string): number | null | undefined {
  if (raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 0 ? n : undefined;
}

function BudgetSection({ canManage }: { canManage: boolean }) {
  const t = useT();
  const { data } = usePersonalDefaultBudget();
  const update = useSetPersonalDefaultBudget();
  const [soft, setSoft] = useState("");
  const [hard, setHard] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (data) {
      setSoft(toDraft(data.softLimitTokens));
      setHard(toDraft(data.hardLimitTokens));
    }
  }, [data]);

  const softValue = parseLimit(soft);
  const hardValue = parseLimit(hard);
  const valid = softValue !== undefined && hardValue !== undefined;
  const unchanged = softValue === data?.softLimitTokens && hardValue === data?.hardLimitTokens;

  const save = async () => {
    if (softValue === undefined || hardValue === undefined) return;
    setError(null);
    try {
      await update.mutateAsync({ softLimitTokens: softValue, hardLimitTokens: hardValue });
      toast.success(t("Default budget saved", "Standard-Budget gespeichert"));
    } catch (err) {
      setError(
        errorText(
          err,
          t(
            "Could not save the default budget.",
            "Das Standard-Budget konnte nicht gespeichert werden.",
          ),
        ),
      );
    }
  };

  return (
    <section aria-label={t("Default budget", "Standard-Budget")}>
      <SectionTitle>{t("Default budget", "Standard-Budget")}</SectionTitle>
      <div className="mt-2 flex flex-wrap gap-4">
        <label className="block text-xs text-muted-foreground">
          {t("Soft limit (tokens)", "Weiches Limit (Tokens)")}
          <input
            type="number"
            min={0}
            value={soft}
            onChange={(e) => setSoft(e.target.value)}
            disabled={!canManage}
            className={INPUT_CLASS}
          />
        </label>
        <label className="block text-xs text-muted-foreground">
          {t("Hard limit (tokens)", "Hartes Limit (Tokens)")}
          <input
            type="number"
            min={0}
            value={hard}
            onChange={(e) => setHard(e.target.value)}
            disabled={!canManage}
            className={INPUT_CLASS}
          />
        </label>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        {t(
          "Leave a field empty for no limit. Applies to every personal department that follows the default.",
          "Lass ein Feld leer für kein Limit. Gilt für jede persönliche Abteilung, die dem Standard folgt.",
        )}
      </p>
      {canManage && (
        <button
          type="button"
          onClick={save}
          disabled={!valid || update.isPending || unchanged}
          className={SAVE_CLASS}
        >
          {update.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Save className="h-3 w-3" />
          )}
          {t("Save", "Speichern")}
        </button>
      )}
      {error && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}

function FrameSection() {
  const t = useT();
  const { data, isLoading, isError } = usePersonalDefaultFrame();
  const setFrame = useSetPersonalDefaultFrame();
  return (
    <>
      <SectionTitle>{t("Default frame", "Standard-Rahmen")}</SectionTitle>
      <div className="mt-2">
        {isLoading ? (
          <p className="text-xs text-muted-foreground">
            {t("Loading guardrails…", "Guardrails werden geladen …")}
          </p>
        ) : isError ? (
          <p role="alert" className="text-xs text-destructive">
            {t(
              "Could not load the default frame.",
              "Der Standard-Rahmen konnte nicht geladen werden.",
            )}
          </p>
        ) : (
          <FrameGuardrailsEditor
            tools={data?.tools ?? {}}
            deviationCounts={data?.deviationCounts ?? {}}
            agentCount={data?.agentCount ?? 0}
            saving={setFrame.isPending}
            saveTools={async (tools) => {
              await setFrame.mutateAsync(tools);
            }}
          />
        )}
      </div>
    </>
  );
}

function PersonalList({ canFollow }: { canFollow: boolean }) {
  const t = useT();
  const { data = [], isLoading, isError } = usePersonalDepartments();
  return (
    <>
      <SectionTitle>
        {t("Existing personal departments", "Bestehende persönliche Abteilungen")}
      </SectionTitle>
      {isLoading ? (
        <p className="mt-2 text-xs text-muted-foreground">{t("Loading…", "Wird geladen …")}</p>
      ) : isError ? (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {t(
            "Could not load the personal departments.",
            "Die persönlichen Abteilungen konnten nicht geladen werden.",
          )}
        </p>
      ) : data.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          {t("No personal departments yet.", "Noch keine persönlichen Abteilungen.")}
        </p>
      ) : (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-[10px] uppercase tracking-widest text-muted-foreground">
              <tr>
                <th className="py-1 pr-3 font-normal">{t("Owner", "Besitzer")}</th>
                <th className="py-1 pr-3 font-normal">{t("Agents", "Agenten")}</th>
                <th className="py-1 pr-3 font-normal">{t("Frame", "Rahmen")}</th>
                <th className="py-1 pr-3 font-normal">{t("Budget", "Budget")}</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <PersonalRow key={row.id} row={row} canFollow={canFollow} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function PersonalRow({ row, canFollow }: { row: PersonalDepartmentAdminDTO; canFollow: boolean }) {
  const t = useT();
  const follow = useFollowPersonalDefault(row.id);
  const [error, setError] = useState<string | null>(null);
  const overridden = !row.followsDefaultFrame || !row.followsDefaultBudget;

  const onFollow = async () => {
    setError(null);
    try {
      await follow.mutateAsync();
      toast.success(t("Now follows the default", "Folgt jetzt dem Standard"));
    } catch (err) {
      const status = (err as { status?: number }).status;
      setError(
        status === 404
          ? t(
              "This is no longer a personal department, or its owner has left.",
              "Das ist keine persönliche Abteilung mehr, oder der Besitzer ist ausgeschieden.",
            )
          : errorText(
              err,
              t("Could not apply the default.", "Der Standard konnte nicht angewendet werden."),
            ),
      );
    }
  };

  return (
    <tr className="border-t border-border/40 align-top">
      <td className="py-2 pr-3">
        <Link to="/departments/$id" params={{ id: row.id }} className="hover:underline">
          {!row.ownerActive
            ? t("Former member", "Ehemaliges Mitglied")
            : row.ownerName || t("Member without a display name", "Mitglied ohne Anzeigenamen")}
        </Link>
        {row.pendingConfirmation && (
          <div className="mt-1">
            <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] text-foreground">
              {t("Waiting for the owner", "Wartet auf die Person")}
            </span>
            {!row.ownerActive && (
              <p className="mt-1 text-[11px] text-muted-foreground">
                {t(
                  "The owner is no longer active, so the change stays held.",
                  "Die Person ist nicht mehr aktiv, die Änderung bleibt daher zurückgehalten.",
                )}
              </p>
            )}
          </div>
        )}
      </td>
      <td className="py-2 pr-3">{row.agentCount}</td>
      <td className="py-2 pr-3">
        {row.followsDefaultFrame
          ? t("Default frame", "Standard-Rahmen")
          : t("Own frame", "Eigener Rahmen")}
      </td>
      <td className="py-2 pr-3">
        {row.followsDefaultBudget
          ? t("Default budget", "Standard-Budget")
          : t("Own budget", "Eigenes Budget")}
      </td>
      <td className="py-2">
        {canFollow && overridden && (
          <button
            type="button"
            onClick={onFollow}
            disabled={follow.isPending}
            className="rounded-md border border-border px-2 py-1 text-xs hover:bg-muted disabled:opacity-40"
          >
            {t("Follow default", "Standard übernehmen")}
          </button>
        )}
        {error && (
          <p role="alert" className="mt-1 text-xs text-destructive">
            {error}
          </p>
        )}
      </td>
    </tr>
  );
}
