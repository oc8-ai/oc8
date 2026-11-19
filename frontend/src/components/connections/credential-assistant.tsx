import { useState } from "react";
import { toast } from "sonner";
import {
  type CredentialCheck,
  type Integration,
  type Owner,
  incompleteFields,
  useCheckCredential,
  useCreateCredentialConnection,
  useUpdateMyCredential,
  useUpdateOrganisationCredential,
} from "@/lib/connections";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import { emptyRequiredFields, loginFieldLabel, loginFieldPlaceholder } from "./login-fields";
import { RequiredMark } from "./required-mark";
import { checkErrorText, incompleteText, saveErrorText } from "./texts";
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
  // Fields the server refused as empty on the last save (422).
  const [refused, setRefused] = useState<string[]>([]);
  const fields = integration.credentialFields;
  // Editing keeps a stored secret when its field stays empty.
  const emptyRequired = emptyRequiredFields(fields, values, { keepSecrets: !!editCredentialId });
  const labelOf = (key: string) => {
    const field = fields.find((f) => f.key === key);
    return field ? loginFieldLabel(field, lang) : key;
  };
  const check = useCheckCredential();
  const create = useCreateCredentialConnection();
  const updateOrganisation = useUpdateOrganisationCredential();
  const updateMine = useUpdateMyCredential();
  const update = owner === "me" ? updateMine : updateOrganisation;
  const busy = check.isPending || create.isPending || update.isPending;

  const setValue = (key: string, value: string) => {
    setValues((current) => ({ ...current, [key]: value }));
    setRefused((current) => current.filter((k) => k !== key));
    setResult(null);
  };

  const runCheck = async () => {
    try {
      setResult(await check.mutateAsync({ integration: integration.name, fieldValues: values }));
    } catch (err) {
      toast.error(saveErrorText(err, t, "check"));
    }
  };

  const save = async () => {
    if (emptyRequired.length > 0) return;
    setRefused([]);
    try {
      if (editCredentialId) await update.mutateAsync({ id: editCredentialId, fieldValues: values });
      else await create.mutateAsync({ integration: integration.name, owner, fieldValues: values });
      toast.success(t(`${integration.label} saved.`, `${integration.label} gespeichert.`));
      onDone();
    } catch (err) {
      const missing = incompleteFields(err);
      if (missing) {
        setRefused(missing);
        return;
      }
      toast.error(saveErrorText(err, t));
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
      {fields.map((field) => {
        const id = `connect-${integration.name}-${field.key}`;
        const hint = whereToFind(integration.credentialType, field.key);
        const empty = !(values[field.key] ?? "").trim();
        const required = !!field.required && !(editCredentialId && field.kind === "password");
        const invalid = (empty && required) || refused.includes(field.key);
        return (
          <div key={field.key} className="space-y-1">
            <label htmlFor={id} className="block text-sm font-medium">
              {loginFieldLabel(field, lang)}
              {required && <RequiredMark className="ml-0.5" />}
            </label>
            <input
              id={id}
              className={`${INPUT} placeholder:italic placeholder:text-muted-foreground/60 ${
                invalid ? "border-amber-500/70" : ""
              }`}
              type={field.kind === "password" ? "password" : field.kind === "url" ? "url" : "text"}
              autoComplete="off"
              required={required}
              aria-invalid={invalid || undefined}
              value={values[field.key] ?? ""}
              placeholder={loginFieldPlaceholder(field, lang)}
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
      {refused.length > 0 && (
        <p role="alert" className="text-sm text-amber-600 dark:text-amber-400">
          {incompleteText(t, integration.label, refused.map(labelOf))}
        </p>
      )}
      {result && (
        <p role="status" className={result.ok ? "text-sm" : "text-sm text-destructive"}>
          {result.ok
            ? t("Connection checked: it works.", "Verbindung geprüft: Sie funktioniert.")
            : checkErrorText(result, t)}
        </p>
      )}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {emptyRequired.length > 0 && (
          <span className="mr-auto text-xs text-muted-foreground">
            {t("Fields marked * are required.", "Mit * markierte Felder sind Pflicht.")}
          </span>
        )}
        <button type="button" onClick={runCheck} disabled={busy} className={SECONDARY}>
          {t("Check connection", "Verbindung prüfen")}
        </button>
        <button type="submit" disabled={busy || emptyRequired.length > 0} className={PRIMARY}>
          {t("Save", "Speichern")}
        </button>
      </div>
    </form>
  );
}
