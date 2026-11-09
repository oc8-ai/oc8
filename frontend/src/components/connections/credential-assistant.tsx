import { useState } from "react";
import { toast } from "sonner";
import {
  type CredentialCheck,
  type Integration,
  type Owner,
  useCheckCredential,
  useCreateCredentialConnection,
  useUpdateOrganisationCredential,
} from "@/lib/connections";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import { checkErrorText } from "./texts";
import { whereToFind } from "./where-to-find";

const INPUT =
  "w-full min-h-11 rounded-md border border-border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";
const SECONDARY =
  "inline-flex min-h-11 items-center rounded-md border border-border px-4 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50";
const PRIMARY =
  "inline-flex min-h-11 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50";

export function CredentialAssistant({
  integration,
  owner,
  editCredentialId,
  onDone,
}: {
  integration: Integration;
  owner: Owner;
  editCredentialId?: string;
  onDone: () => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      integration.credentialFields
        .filter((f) => f.kind !== "password")
        .map((f) => [f.key, f.default ?? ""]),
    ),
  );
  const [result, setResult] = useState<CredentialCheck | null>(null);
  const check = useCheckCredential();
  const create = useCreateCredentialConnection();
  const update = useUpdateOrganisationCredential();
  const busy = check.isPending || create.isPending || update.isPending;

  const setValue = (key: string, value: string) => {
    setValues((current) => ({ ...current, [key]: value }));
    setResult(null);
  };

  const runCheck = async () => {
    try {
      setResult(await check.mutateAsync({ integration: integration.name, fieldValues: values }));
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("The check failed.", "Die Prüfung ist fehlgeschlagen."),
      );
    }
  };

  const save = async () => {
    try {
      if (editCredentialId) await update.mutateAsync({ id: editCredentialId, fieldValues: values });
      else await create.mutateAsync({ integration: integration.name, owner, fieldValues: values });
      toast.success(t(`${integration.label} saved.`, `${integration.label} gespeichert.`));
      onDone();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : t("Saving failed.", "Speichern fehlgeschlagen."),
      );
    }
  };

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      {integration.credentialFields.map((field) => {
        const id = `connect-${integration.name}-${field.key}`;
        const hint = whereToFind(integration.credentialType, field.key);
        return (
          <div key={field.key} className="space-y-1">
            <label htmlFor={id} className="block text-sm font-medium">
              {resolveTranslation(field.label, field.label_translations, lang)}
            </label>
            <input
              id={id}
              className={INPUT}
              type={field.kind === "password" ? "password" : field.kind === "url" ? "url" : "text"}
              autoComplete="off"
              value={values[field.key] ?? ""}
              placeholder={
                field.placeholder
                  ? resolveTranslation(field.placeholder, field.placeholder_translations, lang)
                  : undefined
              }
              onChange={(event) => setValue(field.key, event.target.value)}
            />
            {hint && (
              <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer">
                  {t("Where do I find this?", "Wo finde ich das?")}
                </summary>
                <p className="mt-1">{t(hint.en, hint.de)}</p>
              </details>
            )}
          </div>
        );
      })}
      {result && (
        <p role="status" className={result.ok ? "text-sm" : "text-sm text-destructive"}>
          {result.ok
            ? t("Connection checked: it works.", "Verbindung geprüft: Sie funktioniert.")
            : checkErrorText(result, t)}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" onClick={runCheck} disabled={busy} className={SECONDARY}>
          {t("Check connection", "Verbindung prüfen")}
        </button>
        <button type="submit" disabled={busy} className={PRIMARY}>
          {t("Save", "Speichern")}
        </button>
      </div>
    </form>
  );
}
