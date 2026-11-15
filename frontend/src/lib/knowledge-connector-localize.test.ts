// The connector catalog carries the capa's translations beside its English
// text (`labelTranslations`, `fieldTranslations`, ...); `localizeConnector`
// resolves them for the current language, falling back to the English text.
import { describe, expect, it } from "vitest";
import { localizeConnector, type ConnectorCatalogEntry } from "@/lib/knowledge-connector-hooks";

const ODOO: ConnectorCatalogEntry = {
  typeId: "odoo_knowledge",
  label: "Odoo (Documents & Knowledge)",
  description: "Files from Odoo Documents and articles from Odoo Knowledge",
  configSchema: {
    properties: {
      credential: { type: "string", title: "Odoo login", credentialType: "odoo_login" },
      selection: { type: "array", title: "Folders and articles", description: "What to read" },
      name: { type: "string", title: "Name" },
    },
    required: ["credential", "selection"],
  },
  labelTranslations: { de: "Odoo (Dokumente & Wissen)" },
  descriptionTranslations: { de: "Dateien aus Odoo Dokumente" },
  fieldTranslations: {
    credential: { title: { de: "Odoo-Zugang" } },
    selection: { title: { de: "Ordner und Artikel" }, description: { de: "Was gelesen wird" } },
  },
};

describe("localizeConnector", () => {
  it("shows the German label and field titles in German", () => {
    const de = localizeConnector(ODOO, "de");
    expect(de.label).toBe("Odoo (Dokumente & Wissen)");
    expect(de.description).toBe("Dateien aus Odoo Dokumente");
    expect(de.configSchema.properties?.credential.title).toBe("Odoo-Zugang");
    expect(de.configSchema.properties?.credential.credentialType).toBe("odoo_login");
    expect(de.configSchema.properties?.selection.title).toBe("Ordner und Artikel");
    expect(de.configSchema.properties?.selection.description).toBe("Was gelesen wird");
    expect(de.configSchema.properties?.name.title).toBe("Name");
    expect(de.configSchema.required).toEqual(["credential", "selection"]);
  });

  it("keeps the English text in English and for an untranslated locale", () => {
    for (const lang of ["en", "fr"]) {
      const out = localizeConnector(ODOO, lang);
      expect(out.label).toBe("Odoo (Documents & Knowledge)");
      expect(out.configSchema.properties?.selection.title).toBe("Folders and articles");
    }
  });

  it("leaves a core connector without translations as it is", () => {
    const website: ConnectorCatalogEntry = {
      typeId: "website",
      label: "Website",
      configSchema: { properties: { url: { type: "string", title: "URL" } } },
    };
    expect(localizeConnector(website, "de")).toMatchObject({
      label: "Website",
      configSchema: { properties: { url: { title: "URL" } } },
    });
  });
});
