// Next-step chips under the composer (§5.3 of the AI workplace design).
//
// DETERMINISTIC, computed from state, never model-generated. That is the
// design's own wording and it is the reason this is a pure function in its own
// file rather than a few lines inside a component: no hook, no fetch, no clock,
// no randomness. The same state gives the same chips, so a reader learns what
// they mean instead of re-reading them every time.
//
// Every chip inserts text into the composer. None navigates and none performs
// an action on its own -- a chip that did would be the "parallel menu with a
// different mental model" §5.2 warns about.

export interface SuggestionChip {
  id: string;
  label: string;
  /** Exactly what goes into the composer. A leading command is parsed by the
   *  backend like any other, so `/do …` really does switch mode. */
  insert: string;
}

export interface ChipInputs {
  hasMessages: boolean;
  lastTurnRole: "user" | "assistant" | null;
  /** The mode of the most recent USER turn, or null. */
  lastUserMode: string | null;
  pendingApprovals: number;
  budgetSoftExceeded: boolean;
  mayViewBudget: boolean;
  /** Shipped with the agent (see `AgentDTO.promptStarters`). */
  promptStarters: string[];
}

const MAX_CHIPS = 3;

export function nextStepChips(
  input: ChipInputs,
  t: (en: string, de?: string) => string,
): SuggestionChip[] {
  // An empty conversation is a different question ("what can I even ask?"), so
  // it gets starters rather than next steps.
  if (!input.hasMessages) {
    const starters = input.promptStarters.length
      ? input.promptStarters
      : [
          t("What needs approval?", "Was wartet auf Freigabe?"),
          t("Cost this month?", "Kosten diesen Monat?"),
          t("Create a new agent", "Neuen Agenten anlegen"),
        ];
    return starters.slice(0, MAX_CHIPS).map((text, index) => ({
      id: `starter-${index}`,
      label: text,
      insert: text,
    }));
  }

  const chips: SuggestionChip[] = [];

  // Highest value first: the agent just produced a plan and nothing has been
  // carried out. Gated on the agent having actually answered -- while the last
  // turn is still the user's, the plan does not exist yet.
  if (input.lastUserMode === "plan" && input.lastTurnRole === "assistant") {
    chips.push({
      id: "carry-out-plan",
      label: t("Carry out this plan", "Diesen Plan ausführen"),
      insert: t("/do carry out the plan above", "/do führe den Plan oben aus"),
    });
  }

  if (input.pendingApprovals > 0) {
    chips.push({
      id: "pending-approvals",
      label: t(
        `${input.pendingApprovals} waiting for approval`,
        `${input.pendingApprovals} warten auf Freigabe`,
      ),
      insert: t("What is waiting for my approval?", "Was wartet auf meine Freigabe?"),
    });
  }

  if (input.budgetSoftExceeded && input.mayViewBudget) {
    chips.push({
      id: "budget",
      label: t("Budget is running low", "Budget wird knapp"),
      insert: "/budget",
    });
  }

  chips.push({
    id: "summarise",
    label: t("Summarise this", "Das zusammenfassen"),
    insert: t("/summarise this conversation", "/summarise dieses Gespräch"),
  });

  return chips.slice(0, MAX_CHIPS);
}
