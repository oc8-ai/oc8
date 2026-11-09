import { KeyRound, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  useCreateCredential,
  useCredentials,
  useCredentialTypes,
  useDeleteCredential,
  useTestCredential,
  useUpdateCredential,
  type CredentialDTO,
  type CredentialTypeFieldDTO,
} from "@/lib/hooks";
import { useMay } from "@/lib/governance-hooks";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import { useConfirm } from "@/hooks/use-confirm";
import { ApiError } from "@/lib/api";
import { checkErrorText, saveErrorText } from "./texts";

/** The organisation's other credentials (storage, mail server, model keys):
 *  the former Credentials page, now a section of the Connections page. */
export function OrganisationCredentials({
  excludeTypes = [],
}: {
  excludeTypes?: readonly string[];
}) {
  const t = useT();
  const { lang } = useLang();
  const may = useMay();
  // `secret:view` is the hard gate (the backend endpoints require it);
  // `secret:manage` separately toggles the mutating affordances below (New /
  // Test / Delete), exactly like /models splits `model:view` from
  // `model:manage` rather than hiding the list from view-only callers.
  const mayView = may("secret:view");
  const mayManage = may("secret:manage");
  const [createOpen, setCreateOpen] = useState(false);
  const [editingCredential, setEditingCredential] = useState<CredentialDTO | null>(null);
  const { data: allCredentials = [] } = useCredentials();
  // Integration logins are listed under "Organisation" already; this table
  // keeps everything else (S3, SMTP, model API keys), never a row twice.
  const credentials = allCredentials.filter((c) => !excludeTypes.includes(c.credentialType));
  const { data: types = [] } = useCredentialTypes();
  const deleteCredential = useDeleteCredential();
  const testCredential = useTestCredential();
  const { confirm, ConfirmDialog } = useConfirm();
  const typeLabel = (name: string) => {
    const ty = types.find((entry) => entry.name === name);
    if (!ty) return name;
    return resolveTranslation(ty.displayName, ty.displayNameTranslations, lang);
  };

  // On the Connections page an admin without secret:view simply has no
  // such section.
  if (!mayView) return null;

  return (
    <Panel className="p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
            <KeyRound className="size-4" />
          </div>
          <div>
            <h3 className="font-serif text-lg">
              {t("Further credentials", "Weitere Zugangsdaten")}
            </h3>
            <p className="mt-1 text-sm text-muted-foreground">
              {t(
                "Storage, mail server and model keys -- pick one wherever a Capa or data source needs one.",
                "Speicher, Mailserver und Modellschlüssel -- auswählbar überall, wo eine Capa oder Datenquelle eine braucht.",
              )}
            </p>
          </div>
        </div>
        {mayManage && (
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90"
          >
            <Plus className="size-3.5" />
            {t("New credential", "Neue Zugangsdaten")}
          </button>
        )}
      </div>

      {credentials.length === 0 ? (
        <p className="mt-6 text-sm text-muted-foreground">
          {t("No credentials yet.", "Noch keine Zugangsdaten.")}
        </p>
      ) : (
        <table className="mt-6 w-full text-sm">
          <thead>
            <tr className="border-b border-border">
              <th className="py-3 text-left font-medium text-muted-foreground">
                {t("Name", "Name")}
              </th>
              <th className="py-3 text-left font-medium text-muted-foreground">
                {t("Type", "Typ")}
              </th>
              <th className="py-3 text-left font-medium text-muted-foreground">
                {t("Last tested", "Zuletzt getestet")}
              </th>
              <th className="py-3 text-right font-medium text-muted-foreground">
                {t("Action", "Aktion")}
              </th>
            </tr>
          </thead>
          <tbody>
            {credentials.map((cred) => (
              <tr key={cred.id} className="border-b border-border/50">
                <td className="py-3 font-medium">{cred.name}</td>
                <td className="py-3 text-muted-foreground">{typeLabel(cred.credentialType)}</td>
                <td className="py-3 text-muted-foreground">
                  {cred.lastTestedAt == null
                    ? t("Never tested", "Nie getestet")
                    : cred.lastTestOk
                      ? t("OK", "OK")
                      : t("Failed", "Fehlgeschlagen")}
                </td>
                <td className="py-3 text-right">
                  {mayManage && (
                    <>
                      <button
                        type="button"
                        disabled={testCredential.isPending}
                        onClick={() =>
                          testCredential.mutate(cred.id, {
                            onSuccess: (result) =>
                              result.ok
                                ? toast.success(t("Credential works", "Zugangsdaten funktionieren"))
                                : toast.error(
                                    checkErrorText({ ok: false, reason: null, detail: null }, t),
                                  ),
                            onError: (e: unknown) => toast.error(saveErrorText(e, t, "check")),
                          })
                        }
                        className="mr-2 rounded-md border border-border px-2 py-1 text-xs"
                      >
                        {t("Test", "Testen")}
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditingCredential(cred)}
                        className="mr-2 rounded-md border border-border px-2 py-1 text-xs"
                      >
                        {t("Edit", "Bearbeiten")}
                      </button>
                      <button
                        type="button"
                        disabled={deleteCredential.isPending}
                        onClick={async () => {
                          const ok = await confirm({
                            title: t("Delete credential?", "Zugangsdaten löschen?"),
                            description: t(
                              `Delete "${cred.name}"? Anything still using it will break.`,
                              `"${cred.name}" löschen? Alles, was sie noch nutzt, hört auf zu funktionieren.`,
                            ),
                            confirmLabel: t("Delete", "Löschen"),
                            cancelLabel: t("Cancel", "Abbrechen"),
                          });
                          if (!ok) return;
                          deleteCredential.mutate(cred.id, {
                            onSuccess: () => toast.success(t("Deleted", "Gelöscht")),
                            onError: (e: unknown) =>
                              toast.error(
                                e instanceof ApiError && e.status === 403
                                  ? saveErrorText(e, t)
                                  : t("Could not delete.", "Konnte nicht gelöscht werden."),
                              ),
                          });
                        }}
                        className="rounded-md border border-destructive/40 px-2 py-1 text-xs text-destructive"
                      >
                        {t("Delete", "Löschen")}
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <CreateCredentialDialog open={createOpen} onOpenChange={setCreateOpen} types={types} />
      <EditCredentialDialog
        credential={editingCredential}
        onOpenChange={(open) => {
          if (!open) setEditingCredential(null);
        }}
        types={types}
      />
      {ConfirmDialog}
    </Panel>
  );
}

function CreateCredentialDialog({
  open,
  onOpenChange,
  types,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  types: {
    name: string;
    displayName: string;
    displayNameTranslations: Record<string, string>;
    fields: CredentialTypeFieldDTO[];
  }[];
}) {
  const t = useT();
  const { lang } = useLang();
  const createCredential = useCreateCredential();
  const [name, setName] = useState("");
  const [credentialType, setCredentialType] = useState(types[0]?.name ?? "");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const selectedType = types.find((ty) => ty.name === credentialType);

  const reset = () => {
    setName("");
    setFieldValues({});
  };

  const submit = () => {
    if (!name.trim() || !credentialType) return;
    createCredential.mutate(
      { name: name.trim(), credentialType, fieldValues },
      {
        onSuccess: () => {
          toast.success(t("Credential created", "Zugangsdaten angelegt"));
          reset();
          onOpenChange(false);
        },
        onError: (e: unknown) => toast.error(saveErrorText(e, t)),
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("New credential", "Neue Zugangsdaten")}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-3 text-sm">
          <label className="grid gap-1.5">
            <span>{t("Type", "Typ")}</span>
            <select
              value={credentialType}
              onChange={(e) => setCredentialType(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2"
            >
              {types.map((ty) => (
                <option key={ty.name} value={ty.name}>
                  {resolveTranslation(ty.displayName, ty.displayNameTranslations, lang)}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1.5">
            <span>{t("Name", "Name")}</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2"
              autoFocus
            />
          </label>
          {(selectedType?.fields ?? []).map((field) => (
            <label key={field.key} className="grid gap-1.5">
              <span>{resolveTranslation(field.label, field.label_translations, lang)}</span>
              <input
                type={field.kind === "password" ? "password" : "text"}
                value={fieldValues[field.key] ?? field.default ?? ""}
                onChange={(e) => setFieldValues((cur) => ({ ...cur, [field.key]: e.target.value }))}
                className="rounded-md border border-input bg-background px-3 py-2"
              />
            </label>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            onClick={submit}
            disabled={createCredential.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
          >
            {createCredential.isPending ? t("Creating…", "Wird angelegt…") : t("Create", "Anlegen")}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function EditCredentialDialog({
  credential,
  onOpenChange,
  types,
}: {
  credential: CredentialDTO | null;
  onOpenChange: (open: boolean) => void;
  types: {
    name: string;
    displayName: string;
    displayNameTranslations: Record<string, string>;
    fields: CredentialTypeFieldDTO[];
  }[];
}) {
  const t = useT();
  const { lang } = useLang();
  const updateCredential = useUpdateCredential();
  const [name, setName] = useState("");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const selectedType = types.find((ty) => ty.name === credential?.credentialType);

  // Re-seed local state whenever a different credential is opened for
  // editing -- non-secret fields prefill from what's already stored,
  // secret-kind fields always start blank (the API never returns their
  // values), matching PATCH's own "omitted/blank means unchanged" contract.
  useEffect(() => {
    if (!credential) return;
    setName(credential.name);
    setFieldValues(
      Object.fromEntries(
        Object.entries(credential.fieldValues ?? {}).map(([k, v]) => [k, String(v ?? "")]),
      ),
    );
  }, [credential]);

  if (!credential) return null;

  const submit = () => {
    if (!name.trim()) return;
    const trimmedName = name.trim();
    updateCredential.mutate(
      {
        credentialId: credential.id,
        ...(trimmedName !== credential.name ? { name: trimmedName } : {}),
        fieldValues,
      },
      {
        onSuccess: () => {
          toast.success(t("Credential updated", "Zugangsdaten aktualisiert"));
          onOpenChange(false);
        },
        onError: (e: unknown) => toast.error(saveErrorText(e, t)),
      },
    );
  };

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("Edit credential", "Zugangsdaten bearbeiten")}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-3 text-sm">
          <label className="grid gap-1.5">
            <span>{t("Type", "Typ")}</span>
            <input
              disabled
              value={
                selectedType
                  ? resolveTranslation(
                      selectedType.displayName,
                      selectedType.displayNameTranslations,
                      lang,
                    )
                  : credential.credentialType
              }
              className="rounded-md border border-input bg-muted px-3 py-2 text-muted-foreground"
            />
          </label>
          <label className="grid gap-1.5">
            <span>{t("Name", "Name")}</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2"
              autoFocus
            />
          </label>
          {(selectedType?.fields ?? []).map((field) => (
            <label key={field.key} className="grid gap-1.5">
              <span>{resolveTranslation(field.label, field.label_translations, lang)}</span>
              <input
                type={field.kind === "password" ? "password" : "text"}
                value={fieldValues[field.key] ?? ""}
                onChange={(e) => setFieldValues((cur) => ({ ...cur, [field.key]: e.target.value }))}
                placeholder={
                  field.kind === "password"
                    ? t("Leave blank to keep unchanged", "Leer lassen, um es unverändert zu lassen")
                    : resolveTranslation(field.placeholder, field.placeholder_translations, lang)
                }
                className="rounded-md border border-input bg-background px-3 py-2"
              />
            </label>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            {t("Cancel", "Abbrechen")}
          </button>
          <button
            onClick={submit}
            disabled={updateCredential.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
          >
            {updateCredential.isPending
              ? t("Saving…", "Wird gespeichert…")
              : t("Save", "Speichern")}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
