// E2E F15: the skill drawer's close button was "Close" in German -- the
// shared sheet and dialog never translated their screen-reader label.
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return { ...actual, useT: () => (_en: string, de: string) => de };
});

import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

describe("close buttons in German", () => {
  it("names the sheet's close button in the reader's language", () => {
    render(
      <Sheet open>
        <SheetContent>
          <SheetTitle>Skill</SheetTitle>
        </SheetContent>
      </Sheet>,
    );
    expect(screen.getByRole("button", { name: "Schließen" })).toBeInTheDocument();
  });

  it("names the dialog's close button in the reader's language", () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogTitle>Dialog</DialogTitle>
        </DialogContent>
      </Dialog>,
    );
    expect(screen.getByRole("button", { name: "Schließen" })).toBeInTheDocument();
  });
});
