import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  useCreateCredential,
  useCredentials,
  useCredentialTypes,
  useDeleteCredential,
  useDepartments,
  useTestCredential,
  useUpdateCredential,
  type CredentialDTO,
  type CredentialTypeFieldDTO,
} from "@/lib/hooks";
import { useMay } from "@/lib/governance-hooks";
import { resolveTranslation, useLang, useT } from "@/lib/i18n";
import { useConfirm } from "@/hooks/use-confirm";
import { ApiError } from "@/lib/api";
import {
  type CredentialUsedBy,
  isCredentialInUse,
  useSetCredentialDepartments,
} from "@/lib/connections";
import { ConnectionRow, RowList, RowMenu } from "./connection-row";
import { checkErrorText, fill, inUseDescription, saveErrorText, usedByText } from "./texts";

/** Enables an organisation login for departments, or for all of them: the
 *  one place a login is shared (setups only choose it). */
interface DepartmentsLogin {
  id: string;
  name: string;
  departmentIds?: string[];
  allDepartments?: boolean;
  usedBy?: CredentialUsedBy;
}

export function LoginDepartmentsDialog({
  login,
  onOpenChange,
}: {
  login: DepartmentsLogin | null;
  onOpenChange: (open: boolean) => void;
}) {
  // Keyed by the login: its choice starts from what is saved, and a
  // background refetch never resets what is being edited.
  if (!login) return null;
  return <DepartmentsForm key={login.id} login={login} onOpenChange={onOpenChange} />;
}

function DepartmentsForm({
  login,
  onOpenChange,
}: {
  login: DepartmentsLogin;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const { data: page } = useDepartments({ pageSize: 200 });
  const departments = page?.items ?? [];
  const save = useSetCredentialDepartments();
  const [all, setAll] = useState(() => !!login.allDepartments);
  const [chosen, setChosen] = useState<string[]>(() => login.departmentIds ?? []);

  const toggle = (id: string) =>
    setChosen((cur) => (cur.includes(id) ? cur.filter((d) => d !== id) : [...cur, id]));
  const submit = () =>
    save.mutate(
      { id: login.id, departmentIds: all ? [] : chosen, allDepartments: all },
      {
        onSuccess: () => {
          toast.success(t("Departments saved.", "Abteilungen gespeichert."));
          onOpenChange(false);
        },
        onError: (e: unknown) => toast.error(saveErrorText(e, t)),
      },
    );
  const used = usedByText(t, login.usedBy);

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {fill(t("Departments for {name}", "Abteilungen für {name}"), { name: login.name })}
          </DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          {t(
            "Agents of these departments use this login, unless an agent has chosen another.",
            "Agenten dieser Abteilungen nutzen diesen Zugang, außer ein Agent hat einen anderen gewählt.",
          )}
        </p>
        {used && <p className="text-xs text-muted-foreground">{used}</p>}
        <div className="grid gap-2 text-sm">
          <label className="flex items-center gap-2 font-medium">
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
            {t("All departments", "Alle Abteilungen")}
          </label>
          {departments.map((d) => (
            <label key={d.id} className="flex items-center gap-2 pl-5">
              <input
                type="checkbox"
                disabled={all}
                checked={all || chosen.includes(d.id)}
                onChange={() => toggle(d.id)}
              />
              {d.name}
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
            disabled={save.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
          >
            {save.isPending ? t("Saving…", "Wird gespeichert…") : t("Save", "Speichern")}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

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

  const runTest = (cred: CredentialDTO) =>
    testCredential.mutate(cred.id, {
      onSuccess: (result) =>
        result.ok
          ? toast.success(t("Credential works", "Zugangsdaten funktionieren"))
          : toast.error(checkErrorText({ ok: false, reason: null, detail: null }, t)),
      onError: (e: unknown) => toast.error(saveErrorText(e, t, "check")),
    });

  const remove = async (cred: CredentialDTO) => {
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
    const failed = (e: unknown) =>
      toast.error(
        e instanceof ApiError && e.status === 403
          ? saveErrorText(e, t)
          : t("Could not delete.", "Konnte nicht gelöscht werden."),
      );
    try {
      await deleteCredential.mutateAsync(cred.id);
      toast.success(t("Deleted", "Gelöscht"));
    } catch (e: unknown) {
      if (!isCredentialInUse(e)) return failed(e);
      // Still in use: say by what, and delete only on a second yes.
      const anyway = await confirm({
        title: t("Still in use", "Wird noch genutzt"),
        description: inUseDescription(t, cred.name, cred.usedBy),
        confirmLabel: t("Delete anyway", "Trotzdem löschen"),
        cancelLabel: t("Cancel", "Abbrechen"),
      });
      if (!anyway) return;
      try {
        await deleteCredential.mutateAsync({ credentialId: cred.id, force: true });
        toast.success(t("Deleted", "Gelöscht"));
      } catch (again: unknown) {
        failed(again);
      }
    }
  };

  return (
    <section aria-labelledby="further-credentials" className="mt-6">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 id="further-credentials" className="text-sm font-medium">
            {t("Further credentials", "Weitere Zugangsdaten")}
          </h3>
          <p className="text-xs text-muted-foreground">
            {t(
              "Storage, mail server and model keys, for any Capa or data source that needs one.",
              "Speicher, Mailserver und Modellschlüssel für jede Capa oder Datenquelle, die eine braucht.",
            )}
          </p>
        </div>
        {mayManage && (
          <Button size="sm" variant="outline" onClick={() => setCreateOpen(true)}>
            <Plus />
            {t("New credential", "Neue Zugangsdaten")}
          </Button>
        )}
      </div>

      {credentials.length === 0 ? (
        <p className="py-3 text-sm text-muted-foreground">
          {t("No credentials yet.", "Noch keine Zugangsdaten.")}
        </p>
      ) : (
        <RowList>
          {credentials.map((cred) => {
            const failed = cred.lastTestedAt != null && !cred.lastTestOk;
            return (
              <ConnectionRow
                key={cred.id}
                title={cred.name}
                detail={[typeLabel(cred.credentialType), usedByText(t, cred.usedBy)]
                  .filter(Boolean)
                  .join(" · ")}
                aside={
                  <span className={failed ? "text-amber-600 dark:text-amber-400" : undefined}>
                    {cred.lastTestedAt == null
                      ? t("Never tested", "Nie getestet")
                      : cred.lastTestOk
                        ? t("OK", "OK")
                        : t("Test failed", "Test fehlgeschlagen")}
                  </span>
                }
              >
                {mayManage && (
                  <RowMenu
                    label={t(`Actions for ${cred.name}`, `Aktionen für ${cred.name}`)}
                    items={[
                      {
                        label: t("Test", "Testen"),
                        disabled: testCredential.isPending,
                        onSelect: () => runTest(cred),
                      },
                      {
                        label: t("Edit", "Bearbeiten"),
                        onSelect: () => setEditingCredential(cred),
                      },
                      {
                        label: t("Delete", "Löschen"),
                        destructive: true,
                        disabled: deleteCredential.isPending,
                        onSelect: () => void remove(cred),
                      },
                    ]}
                  />
                )}
              </ConnectionRow>
            );
          })}
        </RowList>
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
    </section>
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
