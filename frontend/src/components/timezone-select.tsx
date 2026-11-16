// The zone a schedule is read in, picked from every IANA zone this browser
// knows. A native input + datalist: compact, and searchable by typing.
import { useEffect, useId, useMemo, useState } from "react";

import { useT } from "@/lib/i18n";
import { timezoneOptions } from "@/lib/timezones";

export function TimezoneSelect({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (zone: string) => void;
  disabled?: boolean;
}) {
  const t = useT();
  const listId = useId();
  const zones = useMemo(() => timezoneOptions(), []);
  // What is typed; only a zone from the list reaches `onChange`, so a half
  // typed name is never saved.
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  return (
    <label className="block">
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {t("Timezone", "Zeitzone")}
      </span>
      <input
        list={listId}
        value={draft}
        disabled={disabled}
        spellCheck={false}
        autoComplete="off"
        onChange={(e) => {
          const next = e.target.value;
          setDraft(next);
          if (zones.includes(next)) onChange(next);
        }}
        onBlur={() => {
          if (!zones.includes(draft)) setDraft(value);
        }}
        className="mt-1 w-full rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm outline-none focus:border-primary/50 disabled:opacity-60"
      />
      <datalist id={listId}>
        {zones.map((zone) => (
          <option key={zone} value={zone} />
        ))}
      </datalist>
    </label>
  );
}
