/** Resolve demo-seed translation overlays against the active UI language.

English stays the canonical API field; locale maps (`roleTranslations`, …)
are only filled for the ACME showcase seed. Live rows leave them empty and
these helpers become no-ops.
*/

import { resolveTranslation, type Lang } from "@/lib/i18n";
import type { ActivityItem, Agent, Department, KnowledgeBase, Task } from "@/lib/mock-data";
import type { Skill } from "@/lib/skills";

function tr(source: string, translations: Record<string, string> | undefined, lang: Lang): string {
  return resolveTranslation(source, translations, lang);
}

function trList(
  source: string[],
  translations: Record<string, string[]> | undefined,
  lang: Lang,
): string[] {
  if (lang === "en" || !translations?.[lang]) return source;
  const localized = translations[lang];
  return localized.length === source.length ? localized : source;
}

export function localizeAgent(a: Agent, lang: Lang): Agent {
  if (lang === "en") return a;
  return {
    ...a,
    role: tr(a.role, a.roleTranslations, lang),
    lastAction: tr(a.lastAction, a.lastActionTranslations, lang),
    lastRun: tr(a.lastRun, a.lastRunTranslations, lang),
    schedule: tr(a.schedule, a.scheduleTranslations, lang),
    guardrails: trList(a.guardrails, a.guardrailsTranslations, lang),
  };
}

export function localizeDepartment(d: Department, lang: Lang): Department {
  if (lang === "en") return d;
  return {
    ...d,
    name: tr(d.name, d.nameTranslations, lang),
    goal: tr(d.goal, d.goalTranslations, lang),
    okr: tr(d.okr, d.okrTranslations, lang),
    kpiLabel: tr(d.kpiLabel, d.kpiLabelTranslations, lang),
  };
}

export function localizeTask(t: Task, lang: Lang): Task {
  if (lang === "en") return t;
  return {
    ...t,
    title: tr(t.title, t.titleTranslations, lang),
    meta: t.meta ? tr(t.meta, t.metaTranslations, lang) : t.meta,
  };
}

export function localizeActivity(a: ActivityItem, lang: Lang): ActivityItem {
  if (lang === "en") return a;
  return {
    ...a,
    message: tr(a.message, a.messageTranslations, lang),
    detail: a.detail ? tr(a.detail, a.detailTranslations, lang) : a.detail,
  };
}

export function localizeLabeled<
  T extends {
    title: string;
    detail: string;
    titleTranslations?: Record<string, string>;
    detailTranslations?: Record<string, string>;
  },
>(row: T, lang: Lang): T {
  if (lang === "en") return row;
  return {
    ...row,
    title: tr(row.title, row.titleTranslations, lang),
    detail: tr(row.detail, row.detailTranslations, lang),
  };
}

export function localizeSkill(s: Skill, lang: Lang): Skill {
  if (lang === "en") return s;
  return {
    ...s,
    source: {
      name: s.name,
      description: s.description,
      instructions: s.instructions,
      guardrails: s.guardrails,
    },
    name: tr(s.name, s.nameTranslations, lang),
    description: tr(s.description, s.descriptionTranslations, lang),
    instructions: tr(s.instructions, s.instructionsTranslations, lang),
    guardrails: trList(s.guardrails, s.guardrailsTranslations, lang),
  };
}

export function localizeKnowledgeBase(kb: KnowledgeBase, lang: Lang): KnowledgeBase {
  if (lang === "en") return kb;
  return {
    ...kb,
    name: tr(kb.name, kb.nameTranslations, lang),
    description: tr(kb.description, kb.descriptionTranslations, lang),
  };
}
