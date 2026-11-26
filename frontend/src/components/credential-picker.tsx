import { LoginFieldInput } from "@/components/connections/login-field-input";
import { useState } from "react";
import { KeyRound } from "lucide-react";
import { toast } from "sonner";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import { incompleteFields } from "@/lib/connections";
import {
  useCreateCredential,
  useCredentials,
  useCredentialTypes,
  type CredentialDTO,
  type CredentialTypeFieldDTO,
} from "@/lib/hooks";
import {
  fill,
  incompleteText,
  noSchemeText,
  usedByAgentsText,
} from "@/components/connections/texts";
import {
  URL_PLACEHOLDER,
  emptyRequiredFields,
  urlFieldsWithoutScheme,
  initialLoginValues,
  loginFieldLabel,
  loginFieldPlaceholder,
} from "@/components/connections/login-fields";
import { RequiredMark } from "@/components/connections/required-mark";

export { URL_PLACEHOLDER };

/** Dropdown of existing credentials of one type, plus an inline "Create
 * new" form -- the unified credentials framework's single reusable picker
 * (design §6), used identically from a Capa's setup form (Task 11) and a
 * connector's config wizard (Task 12). A controlled value/onChange pair,
 * like a native <select>, so neither caller needs to know about the other.
 *
 * `missing`: the chosen login's empty required fields as the server named
 * them (422 `credential_incomplete`), shown with the login's own ones. */
export function CredentialPicker({
  credentialType,
  value,
  onChange,
  missing,
}: {
  credentialType: string;
  value: string;
  onChange: (credentialId: string) => void;
  missing?: readonly string[];
}) {
  const t = useT();
  const { lang } = useLang();
  const { data: credentials = [] } = useCredentials(credentialType);
  const { data: types = [] } = useCredentialTypes();
  const type = types.find((entry) => entry.name === credentialType);
  const fields = type?.fields ?? [];
  const typeDisplayName = type
    ? resolveTranslation(type.displayName, type.displayNameTranslations, lang)
    : credentialType;
  const createCredential = useCreateCredential();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  // Fields the server refused as empty on the last save.
  const [refused, setRefused] = useState<string[]>([]);

  const labelOf = (key: string) => {
    const field = fields.find((f) => f.key === key);
    return field ? loginFieldLabel(field, lang) : key;
  };
  const isEmpty = (field: CredentialTypeFieldDTO) => !(fieldValues[field.key] ?? "").trim();
  const emptyRequired = emptyRequiredFields(fields, fieldValues);
  const noScheme = urlFieldsWithoutScheme(fields, fieldValues);

  const startCreating = () => {
    setFieldValues(initialLoginValues(fields));
    setRefused([]);
    setCreating(true);
  };

  const submit = async () => {
    if (!name.trim() || emptyRequired.length > 0 || noScheme.length > 0) return;
    try {
      const created = await createCredential.mutateAsync({
        name: name.trim(),
        credentialType,
        fieldValues,
      });
      onChange(created.id);
      setCreating(false);
      setName("");
      setFieldValues({});
      setRefused([]);
    } catch (err) {
      const fieldsMissing = incompleteFields(err);
      if (fieldsMissing) {
        setRefused(fieldsMissing);
        return;
      }
      toast.error(t("Could not create credential", "Anmeldedaten konnten nicht erstellt werden"), {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  if (creating) {
    return (
      <div className="space-y-3 rounded-md border border-border bg-background/30 p-3">
        <label className="block text-xs text-muted-foreground">
          <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
            {t("Name", "Name")}
            <RequiredMark className="ml-1" />
          </span>
          <input
            value={name}
            required
            onChange={(e) => setName(e.target.value)}
            placeholder={t(
              `e.g. "Production ${typeDisplayName}"`,
              `z. B. "Produktion ${typeDisplayName}"`,
            )}
            className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50"
          />
        </label>
        {fields.map((field: CredentialTypeFieldDTO) => {
          const label = loginFieldLabel(field, lang);
          const placeholder = loginFieldPlaceholder(field, lang);
          const help = resolveTranslation(field.help, field.help_translations, lang);
          const invalid =
            (isEmpty(field) && field.required) ||
            refused.includes(field.key) ||
            noScheme.includes(field);
          return (
            <label key={field.key} className="block text-xs text-muted-foreground">
              <span className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
                {field.kind === "password" && <KeyRound className="h-3 w-3 text-primary" />}
                {label}
                {field.required && <RequiredMark />}
              </span>
              <LoginFieldInput
                field={field}
                required={field.required}
                aria-invalid={invalid || undefined}
                value={fieldValues[field.key] ?? ""}
                onChange={(next) => {
                  setFieldValues((cur) => ({ ...cur, [field.key]: next }));
                  setRefused((cur) => cur.filter((k) => k !== field.key));
                }}
                placeholder={placeholder}
                className={`w-full rounded-md border bg-background/40 px-3 py-2 text-sm text-foreground outline-none placeholder:italic placeholder:text-muted-foreground/60 focus:border-primary/50 ${
                  invalid ? "border-amber-500/70" : "border-border"
                }`}
              />
              {help && (
                <span className="mt-1 block text-[11px] text-muted-foreground/80">{help}</span>
              )}
            </label>
          );
        })}
        {noScheme.map((field) => (
          <p key={field.key} role="alert" className="text-xs text-amber-600 dark:text-amber-400">
            {noSchemeText(t, loginFieldLabel(field, lang))}
          </p>
        ))}
        {refused.length > 0 && (
          <p role="alert" className="text-xs text-amber-600 dark:text-amber-400">
            {incompleteText(t, name.trim() || typeDisplayName, refused.map(labelOf))}
          </p>
        )}
        <div className="flex items-center justify-end gap-2">
          {emptyRequired.length > 0 && (
            <span className="mr-auto text-[11px] text-muted-foreground">
              {t("Fields marked * are required.", "Mit * markierte Felder sind Pflicht.")}
            </span>
          )}
          <button
            type="button"
            onClick={() => setCreating(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            type="button"
            disabled={
              !name.trim() ||
              emptyRequired.length > 0 ||
              noScheme.length > 0 ||
              createCredential.isPending
            }
            onClick={submit}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
          >
            {createCredential.isPending
              ? t("Saving…", "Wird gespeichert …")
              : t("Save", "Speichern")}
          </button>
        </div>
      </div>
    );
  }

  const optionText = (c: CredentialDTO) => {
    const incomplete = c.incomplete ?? [];
    return [
      incomplete.length > 0 ? incompleteText(t, c.name, incomplete.map(labelOf)) : c.name,
      usedByAgentsText(t, c.usedBy?.agents.length ?? 0),
    ]
      .filter(Boolean)
      .join(" · ");
  };
  const chosen = credentials.find((c) => c.id === value);
  const chosenMissing = [...new Set([...(chosen?.incomplete ?? []), ...(missing ?? [])])];
  const enabledFor = chosen?.departmentIds?.length ?? 0;
  const usedBy = usedByAgentsText(t, chosen?.usedBy?.agents.length ?? 0);

  return (
    <div className="w-full min-w-0 space-y-1.5">
      <div className="flex w-full min-w-0 flex-wrap gap-2">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={chosenMissing.length > 0 || undefined}
          // `min-w-0` overrides the flex item default of `min-width: auto`,
          // which otherwise refuses to shrink a <select> below its widest
          // <option>'s intrinsic width -- that forced the whole row (and the
          // "Create new" button with it) past a narrow parent's edge instead
          // of fitting inside it (live user report: half outside the Hire
          // dialog's tool tile). The 12rem basis is the other half: on a
          // narrow tile the button wraps below instead of squeezing the
          // select down to "Anmel…" (E2E N10).
          className={`min-w-0 flex-[1_1_12rem] rounded-md border bg-background/40 px-3 py-2 text-sm text-foreground ${
            chosenMissing.length > 0 ? "border-amber-500/70" : "border-border"
          }`}
        >
          <option value="">{t("Select a credential…", "Anmeldedaten auswählen …")}</option>
          {credentials.map((c) => (
            <option key={c.id} value={c.id}>
              {optionText(c)}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={startCreating}
          className="shrink-0 rounded-md border border-border px-3 py-2 text-xs text-muted-foreground hover:text-foreground"
        >
          {t("Create new", "Neu erstellen")}
        </button>
      </div>
      {chosen && chosenMissing.length > 0 && (
        <p role="alert" className="text-[11px] text-amber-600 dark:text-amber-400">
          {incompleteText(t, chosen.name, chosenMissing.map(labelOf))}.{" "}
          {t(
            "Complete it under Settings → Connections.",
            "Vervollständige ihn unter Einstellungen → Verbindungen.",
          )}
        </p>
      )}
      {chosen && (
        <p className="text-[11px] text-muted-foreground">
          {chosen.allDepartments
            ? t("Login usable in all departments", "Login in allen Abteilungen nutzbar")
            : enabledFor > 0
              ? fill(
                  t(
                    "Login usable in {count} department(s)",
                    "Login in {count} Abteilung(en) nutzbar",
                  ),
                  { count: String(enabledFor) },
                )
              : t(
                  "Login not usable in any department yet",
                  "Login noch in keiner Abteilung nutzbar",
                )}
          {usedBy && ` · ${usedBy}`}
        </p>
      )}
    </div>
  );
}
