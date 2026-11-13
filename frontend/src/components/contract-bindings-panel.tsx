import { ArrowRight, Link2, Trash2 } from "lucide-react";
import { useMemo } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useConfirm } from "@/hooks/use-confirm";
import {
  useContractBindings,
  useDeleteContractBinding,
  useDepartments,
  useHandoffTypes,
  type BindingDTO,
} from "@/lib/hooks";
import { useT } from "@/lib/i18n";

/** The contract bindings this department takes part in, as sender or receiver.
 *  A binding turns an event this department emits into a handoff for another
 *  one each time the event is emitted; removing it stops that, and leaves the
 *  handoffs it already created alone. */
export function ContractBindingsPanel({ departmentId }: { departmentId: string }) {
  const t = useT();
  const { data: bindings = [] } = useContractBindings();
  const { data: types = [] } = useHandoffTypes();
  // Name lookup over the whole tenant, not a paginated list view.
  const { data: departmentsPage } = useDepartments({ pageSize: 200 });
  const remove = useDeleteContractBinding();
  const { confirm, ConfirmDialog } = useConfirm();

  const deptName = useMemo(() => {
    const map = new Map((departmentsPage?.items ?? []).map((d) => [d.id, d.name]));
    return (id: string) => map.get(id) ?? id;
  }, [departmentsPage]);
  const typeName = useMemo(() => {
    const map = new Map(types.map((ht) => [ht.id, ht.name]));
    return (id: string) => map.get(id) ?? "";
  }, [types]);

  const mine = bindings.filter(
    (b) => b.sourceDepartmentId === departmentId || b.targetDepartmentId === departmentId,
  );

  async function onRemove(b: BindingDTO) {
    const target = deptName(b.targetDepartmentId);
    const ok = await confirm({
      title: t("Remove contract binding?", "Vertragsbindung entfernen?"),
      description: t(
        `"${b.eventType}" will no longer create handoffs for ${target}. Handoffs it already created stay as they are.`,
        `"${b.eventType}" erzeugt dann keine Übergaben mehr für ${target}. Bereits erzeugte Übergaben bleiben, wie sie sind.`,
      ),
      confirmLabel: t("Remove", "Entfernen"),
      cancelLabel: t("Cancel", "Abbrechen"),
    });
    if (!ok) return;
    remove.mutate(b.id, {
      onSuccess: () =>
        toast.success(t("Contract binding removed", "Vertragsbindung entfernt"), {
          description: b.eventType,
        }),
      onError: (error) =>
        toast.error(
          t("Could not remove the binding", "Vertragsbindung konnte nicht entfernt werden"),
          { description: error instanceof Error ? error.message : String(error) },
        ),
    });
  }

  return (
    <Panel className="p-5">
      {ConfirmDialog}
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-serif text-lg">{t("Contract bindings", "Vertragsbindungen")}</h3>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {t("Event to handoff", "Ereignis zu Übergabe")}
        </span>
      </div>
      {mine.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {t(
            "No contract bindings involve this department.",
            "Keine Vertragsbindung betrifft diese Abteilung.",
          )}
        </p>
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border">
          {mine.map((b) => (
            <li key={b.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-xs">
              <Link2 className="h-3.5 w-3.5 text-primary" />
              <span className="font-mono">{b.eventType}</span>
              <span className="text-muted-foreground">{deptName(b.sourceDepartmentId)}</span>
              <ArrowRight className="h-3 w-3 text-muted-foreground" />
              <span className="text-muted-foreground">{deptName(b.targetDepartmentId)}</span>
              {typeName(b.handoffTypeId) && (
                <span className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 font-mono text-[10px]">
                  {typeName(b.handoffTypeId)}
                </span>
              )}
              <button
                type="button"
                onClick={() => onRemove(b)}
                disabled={remove.isPending}
                aria-label={t(
                  `Remove binding ${b.eventType}`,
                  `Vertragsbindung ${b.eventType} entfernen`,
                )}
                className="ml-auto rounded-md p-1 text-muted-foreground transition hover:text-destructive disabled:opacity-50"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
