// frontend/src/components/conversation/plus-menu.tsx
// The composer's `+`: a keyboard menu (DESIGN.md: role="menu", arrows move,
// Enter picks, Esc closes and returns focus). "Upload file" is always first;
// C3 adds data source, knowledge base and oc8 object through `items`.
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Plus } from "lucide-react";

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export interface PlusMenuItem {
  id: string;
  label: string;
  icon: ReactNode;
  onSelect: () => void;
}

export function PlusMenu({
  items,
  disabled = false,
}: {
  items: PlusMenuItem[];
  disabled?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const trigger = useRef<HTMLButtonElement>(null);
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    if (open) refs.current[active]?.focus();
  }, [open, active]);

  function close() {
    setOpen(false);
    trigger.current?.focus();
  }

  function choose(item: PlusMenuItem | undefined) {
    close();
    item?.onSelect();
  }

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length === 0) return;
      setActive((i) => (i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length);
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      setActive(e.key === "Home" ? 0 : Math.max(0, items.length - 1));
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      choose(items[active]);
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  }

  return (
    <div className="relative">
      <button
        ref={trigger}
        type="button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("Add files or context", "Dateien oder Kontext hinzufügen")}
        onClick={() => {
          setActive(0);
          setOpen((v) => !v);
        }}
        className="grid size-9 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60"
      >
        <Plus className="size-4" aria-hidden />
      </button>
      {open && (
        <div
          role="menu"
          aria-label={t("Add", "Hinzufügen")}
          onKeyDown={onKeyDown}
          className="absolute bottom-full left-0 z-50 mb-1 w-64 rounded-lg border border-border bg-popover p-1 shadow-lg"
        >
          {items.map((item, i) => (
            <button
              key={item.id}
              ref={(el) => {
                refs.current[i] = el;
              }}
              type="button"
              role="menuitem"
              tabIndex={i === active ? 0 : -1}
              onClick={() => choose(item)}
              className={cn(
                "flex min-h-11 w-full items-center gap-2 rounded-md px-2 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                i === active && "bg-muted",
              )}
            >
              {item.icon}
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
