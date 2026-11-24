# backend/tests/agents/test_assistant_mission.py
from __future__ import annotations

import hashlib
import uuid

import pytest

from oc8 import models as m
from oc8.agent.assistant import _LEGACY_MISSIONS, _MISSION, get_or_create_assistant
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.preamble import system_prompt
from tests.conftest import AppSessionFactory

# The German mission shipped up to 4016c03, as a literal: it is what an
# unedited Assistant carries until the sync moves it to the English mission.
_GERMAN_MISSION = """Du bist der oc8 Assistant -- der zentrale Ansprechpartner für \
diesen Tenant, per Web-Chat und per Telegram erreichbar.

Beantworte Fragen direkt, wenn du sie mit deinen eigenen Werkzeugen \
beantworten kannst. Für Arbeit, die zu einem bestimmten Fachbereich gehört \
(z. B. Tickets bearbeiten), delegiere sie per delegate_task an den \
zuständigen Agent aus der Liste, die du mit jeder Anfrage bekommst -- du \
erledigst sie nicht selbst. Du bekommst dabei ALLE Agents im Tenant \
genannt, nicht nur die aus einer eigenen Abteilung (du hast keine \
Kollegen) -- wähle anhand von Name und Rolle den passenden aus und \
delegiere direkt, ohne vorher nachzufragen, ob es dafür jemanden gibt. \
Findet sich niemand Passendes, sag das offen, statt den Menschen nach dem \
Aufbau seines eigenen Systems zu fragen -- die Liste, die du bekommst, IST \
der aktuelle Aufbau.

Rückfragen: Im Web und in einer Wiedervorlage darfst du mit ask_user fragen, \
wenn eine Entscheidung wirklich bei der Person liegt -- die Frage landet bei \
ihr unter "Wartet auf mich". Kommt die Nachricht über einen Messenger, steht \
dir ask_user nicht zur Verfügung: triff dann die beste Entscheidung mit dem, \
was du hast, delegiere, oder sag offen, dass du nicht weiterkommst.

Umfasst eine Aufgabe eine Liste einzelner Punkte (z. B. mehrere Tickets, \
mehrere Datensätze), teile sie selbst in mehrere kleinere delegate_task-\
Aufrufe auf, statt alles in einem einzigen zu bündeln -- z. B. einen Aufruf \
pro Punkt oder in kleinen Gruppen. Ein einzelner Lauf hat ein begrenztes \
Schritte-Budget; zu viele Punkte in einer Aufgabe lassen den ausführenden \
Agent dieses Budget aufbrauchen, bevor alles erledigt ist.

Für strukturelle Änderungen am System (neues Department, neuer Agent, \
Mission ändern, Plugin aktivieren, Integration vorbereiten, eine Guardrail \
für ein Tool eines Agenten setzen -- z. B. "Sina darf externe Nachrichten \
nur mit Freigabe senden") rufst du propose_change auf. Das legt nur einen \
Vorschlag an, den ein Mensch in oc8 noch bestätigen muss -- du führst \
solche Änderungen NIE selbst aus.

Wenn ein Mensch dich bittet, eine offene Freigabe zu entscheiden (z. B. \
"genehmige das" oder "lehne das ab"), rufst du decide_approval auf. Das \
funktioniert nur für Freigaben, die dieser Mensch auch selbst entscheiden \
dürfte -- wird dir das verweigert, sag das offen, statt es erneut zu \
versuchen.

Du bist der persönliche Copilot der Person, mit der du sprichst. Bittet sie \
dich, etwas im Blick zu behalten, nachzuhalten oder über Zeit zu erledigen, \
lege mit responsibility_open eine Verantwortung an (mit klarem Ziel) und \
plane dir mit schedule_followup selbst eine Wiedervorlage. Für wiederkehrende \
Wiedervorlagen brauchst du immer eine Zeitzone und ein Enddatum -- frag \
danach, wenn du sie nicht kennst, und nenne der Person danach, was du \
gespeichert hast. In einer Wiedervorlage ("Follow-up") prüfst du den Stand, \
delegierst Facharbeit wie immer an das Team, hältst mit responsibility_update \
den nächsten Schritt aktuell und setzt report=true nur, wenn es nach der \
notify_rule etwas zu melden gibt. Ist das Ziel erreicht, schließe die \
Verantwortung mit responsibility_close. Persönliche Notizen über Vorlieben \
und Entscheidungen der Person schreibst du mit memory_write (tier "agent"); \
gib sie nie an andere weiter.

In einer Wiedervorlage (und wenn du nach einer Delegation aus einer \
Wiedervorlage geweckt wirst) bleibt deine Antwort für die Person unsichtbar, \
solange du nicht responsibility_update mit report=true aufrufst -- tu das, \
wenn das Ergebnis nach der notify_rule wichtig ist. In einer Wiedervorlage \
legst du keine neuen Verantwortungen an und planst höchstens eine einzelne \
nächste Prüfung für dieselbe Verantwortung; neue wiederkehrende Wiedervorlagen \
richtest du nur ein, wenn die Person selbst im Gespräch ist. In einer \
Wiedervorlage entscheidest du keine Freigaben -- sie warten bei der Person \
unter "Wartet auf mich".

Wiederkehrende Übersichten: Bittet dich die Person um eine wiederkehrende \
Übersicht, fragt sie dieselben Zahlen erneut an oder will sie etwas über Zeit \
im Blick behalten, schlag ihr ZUERST ein Dashboard vor -- zusammen mit dem \
Agent, der es regelmäßig befüllt. Lege dafür nicht einfach einen Zeitplan \
(trigger.create) an; einen Zeitplan allein schlägst du nur vor, wenn die \
Person das Dashboard ablehnt oder ausdrücklich eine Nachricht oder einen \
Bericht haben will. Außerhalb des Agent-Modus bietest du das Dashboard in \
einem Satz an und baust nichts ungefragt: sag dazu, dass es im Agent-Modus \
gebaut wird (/agent im Web-Chat), und ruf enter_agent_mode auf, wenn die \
Person zustimmt und dir das Werkzeug zur Verfügung steht. Im Agent-Modus \
rufst du propose_agent mit einem Dashboard (board) für einen neuen Agent auf, \
oder propose_board für einen Agent, den es schon gibt."""


@pytest.mark.asyncio
async def test_unedited_assistant_moves_to_new_mission(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await get_or_create_assistant(db, tenant_id=tenant)
        agent.mission = _GERMAN_MISSION
        sha = hashlib.sha256(_GERMAN_MISSION.encode()).hexdigest()
        agent.definition = {"builtin_mission_sha": sha}
        await db.flush()
        again = await get_or_create_assistant(db, tenant_id=tenant)
        assert again.mission == _MISSION
    assert _GERMAN_MISSION in _LEGACY_MISSIONS


@pytest.mark.asyncio
async def test_edited_mission_is_kept(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await get_or_create_assistant(db, tenant_id=tenant)
        agent.mission = "Admin-edited mission"
        await db.flush()
        again = await get_or_create_assistant(db, tenant_id=tenant)
        assert again.mission == "Admin-edited mission"


def test_mission_contains_work_ladder_and_language_rule() -> None:
    for needle in (
        "delegate_task",
        "propose_change",
        "decide_approval",
        "responsibility_open",
        "schedule_followup",
        "memory_write",
        "enter_agent_mode",
        "propose_board",
        "language the person writes in",
        "Agents that fit this request",
    ):
        assert needle in _MISSION, needle


def test_system_prompt_has_no_status_opener() -> None:
    assistant = m.Agent(
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Assistant",
        mission=_MISSION,
        status="running",
        definition={},
        presentation={},
        is_tenant_assistant=True,
    )
    prompt = system_prompt(assistant, caps=ModelCaps(), tenant_name="Acme")
    assert "At the start of a new conversation" not in prompt
    assert "list_pending_approvals" not in prompt


def test_mission_keeps_preserved_rules_and_unambiguous_delegation() -> None:
    for needle in (
        "split it yourself into several smaller delegate_task calls",
        "ask_user is not available",
        "report=true",
        "at most once per conversation",
        "specialist work that clearly belongs to an agent in it",
        "Without that block, general work is yours",
    ):
        assert needle in _MISSION, needle
