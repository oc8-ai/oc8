"""What each permission MEANS, in words an IT administrator can act on.

`authz/permissions.py` is the truth about which strings exist and which of them
a tenant-defined role may hold. It says nothing a layperson can read: the prose
lives in `#:` comments, is exported nowhere, and the governance screen renders
`permission.split(":")[1]` -- so `supervision:manage`, `handoff:manage`,
`contract:manage` and `flow:manage` are four different rights that all say
"manage" on the form somebody is about to tick.

So every permission gets a hand-written label and one sentence, in German and in
English. **Hand-written per permission, not composed from the resource and the
action.** Composition is what produced the four synonyms above, and it is exactly
what an administrator composing a role cannot resolve: the difference between
"Übergaben verwalten" and "Verträge verwalten" is the whole question they are
being asked.

Two things this module deliberately does NOT do:

* **It does not decide anything.** Nothing here is consulted by a gate. A missing
  entry is a screen with a thin label, never a permission that grants more or
  less than it did -- `DELEGATABLE_PERMISSIONS` remains the only authority on
  what may be offered, and `describe()` reads it rather than restating it.
* **It does not invent a refusal reason.** `delegation_refusal()` owns those, so
  the sentence rendered next to a disabled checkbox is the same sentence
  `POST /roles` puts in its 422. One string, two readers.

`test_the_catalogue_offers_the_delegatable_and_explains_the_rest` holds the
completeness: every permission in `ALL_PERMISSIONS` is described, every refused
one carries its reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from oc8.authz.permissions import ALL_PERMISSIONS, DELEGATABLE_PERMISSIONS, delegation_refusal


@dataclass(frozen=True)
class PermissionInfo:
    """One permission, as a person reads it.

    `label` is what goes on the checkbox; `description` is the sentence
    underneath. Both in both languages: `label`/`description` are English --
    oc8's default -- and `label_de`/`description_de` carry the German
    translation, because the product ships a German screen and an English
    API, and a single-language catalogue would force one of the two to be
    translated at the wrong layer.
    """

    permission: str
    label: str
    description: str
    label_de: str
    description_de: str
    delegatable: bool
    #: Why a tenant-defined role may not hold it, verbatim from
    #: `delegation_refusal`. Empty for anything delegatable.
    reason: str = ""


#: (label_de, description_de, label, description) per permission -- German
#: label/sentence first, then the English (`PermissionInfo`'s default,
#: unsuffixed pair), read out by position in `describe()` below.
#:
#: The order is the order of `sorted(ALL_PERMISSIONS)`, so a new permission's
#: absence is visible in review as a gap in an alphabet rather than as a missing
#: line somewhere in a long dict.
_PROSE: Final[dict[str, tuple[str, str, str, str]]] = {
    "agent:manage": (
        "Agenten einrichten",
        "Agenten anlegen, ihre Werkzeuge einschränken, sie starten und stilllegen.",
        "Configure agents",
        "Create agents, narrow their tools, start and retire them.",
    ),
    "agent:view": (
        "Agenten ansehen",
        "Sieht alle Agenten des Mandanten samt ihrer Werkzeug-Einschränkungen.",
        "View agents",
        "Sees every agent in the tenant, including its tool narrowing.",
    ),
    "agent_version:publish": (
        "Konfiguration veröffentlichen",
        "Macht die bearbeitete Konfiguration eines Agenten zur laufenden Version "
        "-- und setzt sie auf eine frühere zurück.",
        "Publish agent configuration",
        "Turns an agent's edited configuration into the version that runs, and "
        "rolls it back to an earlier one.",
    ),
    "agent_version:view": (
        "Konfigurationsverlauf ansehen",
        "Sieht jede veröffentlichte Version eines Agenten und die Unterschiede "
        "zwischen zwei Versionen.",
        "View configuration history",
        "Sees every published version of an agent and the differences between any two of them.",
    ),
    "approval:decide": (
        "Freigaben entscheiden",
        "Gibt wartende Vorgänge frei oder lehnt sie ab -- in den Abteilungen, "
        "in denen die Person einen Sitz hat.",
        "Decide approvals",
        "Approves or rejects waiting actions, in the departments the person is seated in.",
    ),
    "approval:decide_any": (
        "Freigaben überall entscheiden",
        "Entscheidet in JEDER Abteilung, auch in künftigen -- das hebt die Sitzgrenze auf.",
        "Decide approvals everywhere",
        "Decides in EVERY department, including future ones -- this removes the seat boundary.",
    ),
    "approval:manage": (
        "Freigabe-Regeln verwalten",
        "Ändert, wann überhaupt eine Freigabe verlangt wird (Schwellen, Ausnahmen).",
        "Manage approval rules",
        "Changes when an approval is required at all (thresholds, exemptions).",
    ),
    "approval:view": (
        "Freigaben ansehen",
        "Sieht die wartenden Vorgänge der eigenen Abteilungen.",
        "View approvals",
        "Sees the waiting actions of their own departments.",
    ),
    "approval:view_any": (
        "Freigaben überall ansehen",
        "Sieht die Freigaben JEDER Abteilung, auch künftiger -- das hebt die Sitzgrenze auf.",
        "View approvals everywhere",
        "Sees EVERY department's approvals, including future ones -- this removes "
        "the seat boundary.",
    ),
    "audit:verify": (
        "Prüfprotokoll verifizieren",
        "Rechnet die Hash-Kette des Protokolls nach und meldet Lücken.",
        "Verify the audit trail",
        "Recomputes the trail's hash chain and reports gaps.",
    ),
    "audit:view": (
        "Prüfprotokoll lesen",
        "Liest, wer wann was entschieden hat -- einschließlich der eigenen Vorgesetzten.",
        "Read the audit trail",
        "Reads who decided what and when -- including their own superiors.",
    ),
    "backup:export": (
        "Sicherung exportieren",
        "Lädt eine vollständige Kopie des Mandanten herunter -- jedes Protokoll, jeden "
        "Wissens-Baustein und, mit einer Passphrase, jedes hinterlegte Zugangsdatum.",
        "Export a backup",
        "Downloads a complete copy of the tenant -- every transcript, every knowledge "
        "chunk and, with a passphrase, every credential the company holds.",
    ),
    "backup:restore": (
        "Sicherung wiederherstellen",
        "Ersetzt jeden Agenten, jeden Lauf und jeden Wissens-Baustein des Mandanten "
        "durch den Inhalt einer hochgeladenen Datei.",
        "Restore a backup",
        "Replaces every agent, run and knowledge chunk in the tenant with the "
        "contents of an uploaded file.",
    ),
    "budget:manage": (
        "Budgets festlegen",
        "Setzt Kostengrenzen und hebt sie auf; eine aufgehobene Grenze lässt Agenten weiterlaufen.",
        "Set budgets",
        "Sets and lifts cost caps; a lifted cap lets agents keep spending.",
    ),
    "budget:view": (
        "Kosten ansehen",
        "Sieht den Verbrauch gegen die gesetzten Grenzen.",
        "View costs",
        "Sees spend against the caps that are set.",
    ),
    "channel:manage": (
        "Messenger-Kanäle verwalten",
        "Verbindet Telegram und andere Kanäle und entscheidet, wer darüber freigeben darf.",
        "Manage messenger channels",
        "Connects Telegram and other channels and decides who may approve through them.",
    ),
    "channel:view": (
        "Messenger-Kanäle ansehen",
        "Sieht, welche Kanäle verbunden sind und wer daran hängt.",
        "View messenger channels",
        "Sees which channels are connected and who is attached to them.",
    ),
    "clarification:answer": (
        "Rückfragen beantworten",
        "Beantwortet die Fragen, an denen ein Agent hängen geblieben ist, und setzt ihn fort.",
        "Answer clarifications",
        "Answers the questions an agent got stuck on, and resumes it.",
    ),
    "clarification:view": (
        "Rückfragen ansehen",
        "Sieht die offenen Fragen der eigenen Abteilungen.",
        "View clarifications",
        "Sees the open questions of their own departments.",
    ),
    "contract:manage": (
        "Leistungsversprechen ändern",
        "Ändert, was eine Abteilung zusagt: Bearbeitungszeiten und Qualitätszusagen.",
        "Change service contracts",
        "Changes what a department promises: turnaround times and quality commitments.",
    ),
    "contract:view": (
        "Leistungsversprechen ansehen",
        "Sieht die zugesagten Bearbeitungszeiten und ob sie gehalten werden.",
        "View service contracts",
        "Sees the promised turnaround times and whether they are being met.",
    ),
    "department:manage": (
        "Abteilungen einrichten",
        "Legt Abteilungen an, benennt sie um und archiviert sie -- mitsamt allem, was daran hängt.",
        "Configure departments",
        "Creates, renames and archives departments -- along with everything attached to them.",
    ),
    "department:view": (
        "Abteilungen ansehen",
        "Sieht die Abteilungstafeln des ganzen Mandanten.",
        "View departments",
        "Sees the department boards of the whole tenant.",
    ),
    "flow:manage": (
        "Abläufe ändern",
        "Ändert die mehrstufigen Abläufe, in denen Agenten einander übergeben.",
        "Change workflows",
        "Changes the multi-step flows in which agents hand work to one another.",
    ),
    "flow:view": (
        "Abläufe ansehen",
        "Sieht, welche Abläufe definiert sind und wo sie gerade stehen.",
        "View workflows",
        "Sees which flows are defined and where they currently stand.",
    ),
    "handoff:manage": (
        "Übergaben steuern",
        "Leitet Arbeit von einer Abteilung an eine andere weiter oder holt sie zurück.",
        "Control handoffs",
        "Routes work from one department to another, or takes it back.",
    ),
    "handoff:view": (
        "Übergaben ansehen",
        "Sieht jede Übergabe im Mandanten und beide beteiligten Abteilungen.",
        "View handoffs",
        "Sees every handoff in the tenant and both departments involved.",
    ),
    "integration:manage": (
        "Fremdsysteme anbinden",
        "Trägt ein, mit welchem Befehl und welchen Zugangsdaten ein Fremdsystem gestartet wird.",
        "Connect external systems",
        "Records which command and which credentials start an external system.",
    ),
    "integration:view": (
        "Fremdsysteme ansehen",
        "Sieht die Adressen und Startbefehle der angebundenen Systeme.",
        "View external systems",
        "Sees the addresses and start-up commands of the connected systems.",
    ),
    "knowledge:manage": (
        "Wissen pflegen",
        "Lädt Dokumente hoch, verbindet Quellen und löscht Wissen wieder.",
        "Curate knowledge",
        "Uploads documents, connects sources, and deletes knowledge again.",
    ),
    "knowledge:view": (
        "Wissen ansehen",
        "Liest die Dokumente, auf die sich die Agenten stützen.",
        "View knowledge",
        "Reads the documents the agents rely on.",
    ),
    "member:manage": (
        "Personen und Rollen zuweisen",
        "Nimmt Personen auf, gibt ihnen Rollen und setzt sie in Abteilungen.",
        "Assign people and roles",
        "Enrols people, gives them roles, and seats them in departments.",
    ),
    "member:view": (
        "Personen ansehen",
        "Sieht, wen dieser Mandant kennt und wo die Person sitzt.",
        "View people",
        "Sees who this tenant knows and where each of them is seated.",
    ),
    "model:manage": (
        "Sprachmodelle einrichten",
        "Trägt ein, welches Modell wo läuft -- und damit, ob Daten das Haus verlassen.",
        "Configure language models",
        "Records which model runs where -- and therefore whether data leaves the building.",
    ),
    "model:view": (
        "Sprachmodelle ansehen",
        "Sieht, welche Modelle eingerichtet sind und was sie kosten.",
        "View language models",
        "Sees which models are configured and what they cost.",
    ),
    "copilot:manage": (
        "Konfigurations-Copilot nutzen",
        "Chattet mit dem Copilot und wendet dessen Vorschläge an -- mandantenweit, "
        "über jede Abteilung hinweg.",
        "Use the configuration Copilot",
        "Chats with the Copilot and applies its proposals -- tenant-wide, across every department.",
    ),
    "copilot:use": (
        "Assistenten-Chat öffnen",
        "Öffnet den oc8-Assistenten und nutzt ihn zum Chatten -- mandantenweit.",
        "Open the assistant chat",
        "Opens the oc8 Assistant and uses it for chatting -- tenant-wide.",
    ),
    "copilot:view": (
        "Copilot-Vorschläge ansehen",
        "Liest einen vorbereiteten Copilot-Vorschlag, bevor er angewendet wird.",
        "View Copilot proposals",
        "Reads a prepared Copilot proposal before it is applied.",
    ),
    "plugin:manage": (
        "Erweiterungen installieren",
        "Installiert Erweiterungen; deren Code läuft anschließend im Backend selbst.",
        "Install plugins",
        "Installs plugins; their code then runs inside the backend itself.",
    ),
    "plugin:view": (
        "Erweiterungen ansehen",
        "Sieht, welche Erweiterungen installiert und aktiv sind.",
        "View plugins",
        "Sees which plugins are installed and active.",
    ),
    "role:manage": (
        "Rollen anlegen",
        "Legt eigene Rollen an und ändert, was sie dürfen.",
        "Author roles",
        "Creates the tenant's own roles and changes what they may do.",
    ),
    "role:view": (
        "Rollen ansehen",
        "Sieht die Rollen dieses Mandanten und wer sie hat.",
        "View roles",
        "Sees this tenant's roles and who holds them.",
    ),
    "run:control": (
        "Läufe steuern",
        "Hält einen laufenden Agentenlauf an oder bricht ihn ab.",
        "Control runs",
        "Pauses or cancels a running agent run.",
    ),
    "run:manage": (
        "Läufe verwalten",
        "Ändert und entfernt Läufe samt ihrer Aufzeichnung.",
        "Manage runs",
        "Alters and removes runs together with their record.",
    ),
    "run:start": (
        "Läufe starten",
        "Lässt einen Agenten eine Aufgabe übernehmen.",
        "Start runs",
        "Lets an agent take on a task.",
    ),
    "run:view": (
        "Läufe im Detail lesen",
        "Liest das vollständige Protokoll eines Laufs samt jedem Werkzeugaufruf -- mandantenweit.",
        "Read run transcripts",
        "Reads a run's full transcript including every tool call -- tenant-wide.",
    ),
    "secret:manage": (
        "Zugangsdaten ändern",
        "Hinterlegt und tauscht Zugangsdaten; eine getauschte zeigt auf ein anderes System.",
        "Change credentials",
        "Stores and rotates credentials; a rotated one points at a different system.",
    ),
    "secret:view": (
        "Zugangsdaten auflisten",
        "Sieht, welche Zugangsdaten hinterlegt sind -- also überall, wohin dieser Mandant reicht.",
        "List credentials",
        "Sees which credentials are stored -- that is, everywhere this tenant can reach.",
    ),
    "settings:manage": (
        "Grundeinstellungen ändern",
        "Ändert die mandantenweiten Schalter, darunter den, der neue Agenten überhaupt zulässt.",
        "Change core settings",
        "Changes the tenant-wide switches, including the one that admits new agents at all.",
    ),
    "settings:view": (
        "Grundeinstellungen ansehen",
        "Sieht die mandantenweiten Schalter, ohne sie ändern zu können.",
        "View core settings",
        "Sees the tenant-wide switches without being able to change them.",
    ),
    "skill:manage": (
        "Fähigkeiten pflegen",
        "Schreibt die Arbeitsanweisungen, nach denen Agenten vorgehen.",
        "Curate skills",
        "Writes the working instructions agents follow.",
    ),
    "skill:view": (
        "Fähigkeiten ansehen",
        "Liest die Arbeitsanweisungen der Agenten.",
        "View skills",
        "Reads the agents' working instructions.",
    ),
    "statistics:view": (
        "Statistiken ansehen",
        "Sieht die live berechneten Kennzahlen aller Agenten und Abteilungen des Mandanten "
        "auf einmal.",
        "View statistics",
        "Sees the live-computed performance numbers across every agent and department "
        "in the tenant at once.",
    ),
    "supervision:manage": (
        "Aufsicht einstellen",
        "Legt fest, wie streng Agenten beaufsichtigt werden und wann eingegriffen wird.",
        "Configure supervision",
        "Decides how closely agents are supervised and when somebody steps in.",
    ),
    "supervision:view": (
        "Aufsicht ansehen",
        "Sieht die Aufsichtsbefunde und die Eingriffe.",
        "View supervision",
        "Sees the supervision findings and the interventions.",
    ),
    "tool:read": (
        "Werkzeugrecht (lesen)",
        "Recht eines AGENTEN, in Fremdsystemen zu lesen. Keine Person hält es.",
        "Tool right (read)",
        "An AGENT's right to read in external systems. No person holds it.",
    ),
    "tool:send": (
        "Werkzeugrecht (senden)",
        "Recht eines AGENTEN, nach außen zu senden (E-Mail, Angebot). Keine Person hält es.",
        "Tool right (send)",
        "An AGENT's right to send outward (email, quotation). No person holds it.",
    ),
    "tool:write": (
        "Werkzeugrecht (schreiben)",
        "Recht eines AGENTEN, in Fremdsystemen zu schreiben. Keine Person hält es.",
        "Tool right (write)",
        "An AGENT's right to write in external systems. No person holds it.",
    ),
    "trigger:manage": (
        "Auslöser einrichten",
        "Legt fest, wann Agenten von selbst loslaufen -- zeitgesteuert oder auf ein Ereignis.",
        "Configure triggers",
        "Decides when agents start on their own -- on a schedule or on an event.",
    ),
    "trigger:view": (
        "Auslöser ansehen",
        "Sieht, welche Auslöser eingerichtet sind und wann sie zuletzt gefeuert haben.",
        "View triggers",
        "Sees which triggers exist and when they last fired.",
    ),
}


def describe(permission: str) -> PermissionInfo:
    """One permission as the screen shows it.

    Falls back to the string itself rather than raising. This is a label lookup
    on a diagnostic screen: a permission added without prose must show up as a
    thin row somebody notices, not as a 500 on the page that explains refusals.
    `test_the_catalogue_offers_the_delegatable_and_explains_the_rest` is what
    makes the gap visible in CI instead of in production.
    """
    label_de, description_de, label, description = _PROSE.get(
        permission, (permission, "", permission, "")
    )
    return PermissionInfo(
        permission=permission,
        label=label,
        description=description,
        label_de=label_de,
        description_de=description_de,
        delegatable=permission in DELEGATABLE_PERMISSIONS,
        reason=delegation_refusal(permission) or "",
    )


def catalogue() -> list[PermissionInfo]:
    """Every permission this deployment knows, described, sorted.

    Refused entries are INCLUDED, not filtered out. The screen renders them
    disabled with their reason beside them: a hidden control produces a support
    ticket asking where the setting went, and explaining its own refusal is the
    whole ethos of the layer this belongs to.
    """
    return [describe(p) for p in sorted(ALL_PERMISSIONS)]
