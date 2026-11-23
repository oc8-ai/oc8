import { useEffect, type InputHTMLAttributes } from "react";

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "type"> & {
  field: { kind: string; default?: string; options?: string[] };
  value: string;
  onChange: (value: string) => void;
};

/** Protocol and encryption names a login type lists as raw values. */
const KNOWN_OPTIONS: Record<string, string> = {
  imap: "IMAP",
  pop3: "POP3",
  smtp: "SMTP",
  ssl: "SSL/TLS",
  tls: "TLS",
  starttls: "STARTTLS",
  none: "None",
};

/** A select choice as the person reads it: a known protocol name, else the
 *  raw value humanised (`read_only` -> "Read only"). The value submitted
 *  stays the raw one (E2E N10). */
export function optionLabel(option: string): string {
  const known = KNOWN_OPTIONS[option.toLowerCase()];
  if (known) return known;
  const words = option.replace(/[_-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : option;
}

/** One login field's control: a `select` field (a login type's fixed
 *  choices, e.g. a mailbox's protocol) as a select, everything else as an
 *  input of its kind. Every form that edits a login renders its fields
 *  through this, so a field kind is not a text box in one of them. */
export function LoginFieldInput({ field, value, onChange, placeholder, ...rest }: Props) {
  const isSelect = field.kind === "select";
  const seed = isSelect && !value && field.default ? field.default : null;
  // A select shows its default as chosen: put it into the form's state too,
  // so a login saved without touching it submits what it showed.
  useEffect(() => {
    if (seed !== null) onChange(seed);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- seed once per value
  }, [seed]);

  if (isSelect) {
    const options = field.options ?? [];
    const current = value || field.default || "";
    return (
      <select
        id={rest.id}
        name={rest.name}
        disabled={rest.disabled}
        required={rest.required}
        autoFocus={rest.autoFocus}
        aria-invalid={rest["aria-invalid"]}
        aria-label={rest["aria-label"]}
        aria-describedby={rest["aria-describedby"]}
        className={rest.className}
        value={current}
        onChange={(event) => onChange(event.target.value)}
      >
        {!options.includes(current) && <option value={current}>{optionLabel(current)}</option>}
        {options.map((option) => (
          <option key={option} value={option}>
            {optionLabel(option)}
          </option>
        ))}
      </select>
    );
  }
  return (
    <input
      {...rest}
      type={field.kind === "password" ? "password" : field.kind === "url" ? "url" : "text"}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}
