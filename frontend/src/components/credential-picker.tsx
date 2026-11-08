import { useState } from "react";
import { KeyRound } from "lucide-react";
import { toast } from "sonner";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import {
  useCreateCredential,
  useCredentials,
  useCredentialTypes,
  type CredentialTypeFieldDTO,
} from "@/lib/hooks";

/** Dropdown of existing credentials of one type, plus an inline "Create
 * new" form -- the unified credentials framework's single reusable picker
 * (design §6), used identically from a Capa's setup form (Task 11) and a
 * connector's config wizard (Task 12). A controlled value/onChange pair,
 * like a native <select>, so neither caller needs to know about the other. */
export function CredentialPicker({
  credentialType,
  value,
  onChange,
}: {
  credentialType: string;
  value: string;
  onChange: (credentialId: string) => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const { data: credentials = [] } = useCredentials(credentialType);
  const { data: types = [] } = useCredentialTypes();
  const type = types.find((entry) => entry.name === credentialType);
  const typeDisplayName = type
    ? resolveTranslation(type.displayName, type.displayNameTranslations, lang)
    : credentialType;
  const createCredential = useCreateCredential();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});

  const submit = async () => {
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
    } catch (err) {
      toast.error(t("Could not create credential", "Anmeldedaten konnten nicht erstellt werden"), {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  };

  if (creating) {
    return (
      <div className="space-y-3 rounded-md border border-border bg-background/30 p-3">
        <label className="block text-xs text-muted-foreground">
          <span className="mb-1 block micro-label">{t("Name", "Name")}</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t(
              `e.g. "Production ${typeDisplayName}"`,
              `z. B. "Produktion ${typeDisplayName}"`,
            )}
            className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50"
          />
        </label>
        {(type?.fields ?? []).map((field: CredentialTypeFieldDTO) => {
          const label = resolveTranslation(field.label, field.label_translations, lang);
          const placeholder = resolveTranslation(
            field.placeholder,
            field.placeholder_translations,
            lang,
          );
          const help = resolveTranslation(field.help, field.help_translations, lang);
          return (
            <label key={field.key} className="block text-xs text-muted-foreground">
              <span className="mb-1 flex items-center gap-1.5 micro-label">
                {field.kind === "password" && <KeyRound className="h-3 w-3 text-primary" />}
                {label}
              </span>
              <input
                type={
                  field.kind === "password" ? "password" : field.kind === "url" ? "url" : "text"
                }
                value={fieldValues[field.key] ?? field.default ?? ""}
                onChange={(e) => setFieldValues((cur) => ({ ...cur, [field.key]: e.target.value }))}
                placeholder={placeholder}
                className="w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50"
              />
              {help && <span className="mt-1 block text-xs text-muted-foreground/80">{help}</span>}
            </label>
          );
        })}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setCreating(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            type="button"
            disabled={!name.trim() || createCredential.isPending}
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

  return (
    <div className="flex w-full min-w-0 flex-wrap gap-2">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        // `min-w-0` overrides the flex item default of `min-width: auto`,
        // which otherwise refuses to shrink a <select> below its widest
        // <option>'s intrinsic width -- that forced the whole row (and the
        // "Create new" button with it) past a narrow parent's edge instead
        // of fitting inside it (live user report: half outside the Hire
        // dialog's tool tile).
        className="min-w-0 flex-1 rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground"
      >
        <option value="">{t("Select a credential…", "Anmeldedaten auswählen …")}</option>
        {credentials.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={() => setCreating(true)}
        className="shrink-0 rounded-md border border-border px-3 py-2 text-xs text-muted-foreground hover:text-foreground"
      >
        {t("Create new", "Neu erstellen")}
      </button>
    </div>
  );
}
