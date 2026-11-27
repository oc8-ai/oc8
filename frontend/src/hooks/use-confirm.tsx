import { type ReactNode, useCallback, useRef, useState } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface ConfirmOptions {
  title: string;
  description: string;
  confirmLabel: string;
  cancelLabel: string;
  // Every current caller confirms an irreversible delete/remove, so this
  // defaults to true rather than making every call site repeat it.
  destructive?: boolean;
  /** Extra content under the description, e.g. an opt-in checkbox. */
  body?: ReactNode;
}

// Replaces window.confirm() with an oc8-styled AlertDialog while keeping each
// call site's shape ("if (!(await confirm(...))) return;") unchanged. One
// dialog instance per hook call -- render `<ConfirmDialog />` once in the
// component that calls `confirm`, not once per row/item.
export function useConfirm() {
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const resolveRef = useRef<((value: boolean) => void) | null>(null);

  const confirm = useCallback((opts: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      resolveRef.current = resolve;
      setOptions(opts);
    });
  }, []);

  const settle = useCallback((value: boolean) => {
    resolveRef.current?.(value);
    resolveRef.current = null;
    setOptions(null);
  }, []);

  const ConfirmDialog = (
    <AlertDialog open={options !== null} onOpenChange={(open) => !open && settle(false)}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{options?.title}</AlertDialogTitle>
          <AlertDialogDescription>{options?.description}</AlertDialogDescription>
        </AlertDialogHeader>
        {options?.body}
        <AlertDialogFooter>
          <AlertDialogCancel onClick={() => settle(false)}>
            {options?.cancelLabel}
          </AlertDialogCancel>
          <AlertDialogAction
            className={cn(
              options?.destructive !== false && buttonVariants({ variant: "destructive" }),
            )}
            onClick={() => settle(true)}
          >
            {options?.confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );

  return { confirm, ConfirmDialog };
}
