// The rules every form that creates or edits a tool login follows (the
// credential picker in setups, the Connections page's login assistant), so
// the two cannot drift: what a field shows is what is sent, a `url` field
// never shows an address that could pass for a value, and a required field
// is marked and blocks saving while empty.
import { resolveTranslation } from "@/lib/i18n";

/** A `url` field's placeholder: an address that cannot be mistaken for a
 *  value already filled in (a local address looked like one, and an empty
 *  URL got saved). */
export const URL_PLACEHOLDER = "https://your-company.example.com";

export interface LoginField {
  key: string;
  label: string;
  kind: string;
  required?: boolean;
  default?: string;
  placeholder?: string;
  label_translations?: Record<string, string>;
  placeholder_translations?: Record<string, string>;
}

/** The placeholder a login field shows. */
export function loginFieldPlaceholder(field: LoginField, lang: string): string | undefined {
  if (field.kind === "url") return URL_PLACEHOLDER;
  return field.placeholder
    ? resolveTranslation(field.placeholder, field.placeholder_translations, lang)
    : undefined;
}

/** The field's label in the viewer's language. */
export function loginFieldLabel(field: LoginField, lang: string): string {
  return resolveTranslation(field.label, field.label_translations, lang);
}

/** The defaults a new login starts with, put into the form's state. */
export function initialLoginValues(fields: readonly LoginField[]): Record<string, string> {
  return Object.fromEntries(
    fields.filter((f) => f.default).map((f) => [f.key, String(f.default)] as const),
  );
}

/** The required fields left empty. `keepSecrets`: editing a stored login,
 *  where an empty secret field keeps the stored secret. */
export function emptyRequiredFields(
  fields: readonly LoginField[],
  values: Readonly<Record<string, string>>,
  { keepSecrets = false }: { keepSecrets?: boolean } = {},
): LoginField[] {
  return fields.filter(
    (f) => f.required && !(keepSecrets && f.kind === "password") && !(values[f.key] ?? "").trim(),
  );
}
