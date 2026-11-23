import type { InputHTMLAttributes } from "react";

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "type"> & {
  field: { kind: string; default?: string; options?: string[] };
  value: string;
  onChange: (value: string) => void;
};

/** One login field's control: a `select` field (a login type's fixed
 *  choices, e.g. a mailbox's protocol) as a select, everything else as an
 *  input of its kind. Every form that edits a login renders its fields
 *  through this, so a field kind is not a text box in one of them. */
export function LoginFieldInput({ field, value, onChange, placeholder, ...rest }: Props) {
  if (field.kind === "select") {
    const options = field.options ?? [];
    const current = value || field.default || "";
    return (
      <select
        id={rest.id}
        required={rest.required}
        aria-invalid={rest["aria-invalid"]}
        aria-label={rest["aria-label"]}
        className={rest.className}
        value={current}
        onChange={(event) => onChange(event.target.value)}
      >
        {!options.includes(current) && <option value={current}>{current}</option>}
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
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
