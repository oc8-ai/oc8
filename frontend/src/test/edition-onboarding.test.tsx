import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  EditionCompositionProvider,
  composeFrontendEdition,
  editionBarePaths,
  onboardingRedirect,
} from "@/edition/composition";
import { EditionProfileSections, EditionSidebarFooter, EditionSidebarHeader } from "@/edition/slots";

const onboarding = { adminRoute: "/edition-setup", memberRoute: "/edition-hello" };

describe("edition onboarding composition", () => {
  it("takes the one extension that supplies onboarding", () => {
    expect(composeFrontendEdition([{ id: "a" }, { id: "b", onboarding }]).onboarding).toEqual(onboarding);
    expect(composeFrontendEdition([]).onboarding).toBeNull();
  });

  it("refuses a second onboarding, sidebar header, sidebar footer or profile section id", () => {
    const C = () => null;
    expect(() => composeFrontendEdition([{ id: "a", onboarding }, { id: "b", onboarding }])).toThrow(
      "Duplicate edition onboarding: b",
    );
    expect(() =>
      composeFrontendEdition([{ id: "a", sidebarHeader: C }, { id: "b", sidebarHeader: C }]),
    ).toThrow("Duplicate edition sidebar header: b");
    expect(() =>
      composeFrontendEdition([{ id: "a", sidebarFooter: C }, { id: "b", sidebarFooter: C }]),
    ).toThrow("Duplicate edition sidebar footer: b");
    expect(() =>
      composeFrontendEdition([
        { id: "a", profileSections: [{ id: "x", component: C }] },
        { id: "b", profileSections: [{ id: "x", component: C }] },
      ]),
    ).toThrow("Duplicate edition profile section: x");
  });

  it("lists the onboarding routes as paths without the app shell", () => {
    expect(editionBarePaths({ onboarding })).toEqual(["/edition-setup", "/edition-hello"]);
    expect(editionBarePaths({ onboarding: { adminRoute: "/only-admin" } })).toEqual(["/only-admin"]);
    expect(editionBarePaths({ onboarding: null })).toEqual([]);
  });
});

describe("onboarding redirect", () => {
  it("keeps Community's /welcome for a pending admin without an edition", () => {
    expect(onboardingRedirect({ onboardingStatus: "pending" }, true, null)).toBe("/welcome");
  });

  it("sends a pending admin to the edition's admin route", () => {
    expect(onboardingRedirect({ onboardingStatus: "pending" }, true, onboarding)).toBe("/edition-setup");
  });

  it("sends a member whose own welcome is pending to the member route", () => {
    expect(
      onboardingRedirect({ onboardingStatus: "completed", memberOnboardingStatus: "pending" }, false, onboarding),
    ).toBe("/edition-hello");
    expect(
      onboardingRedirect({ onboardingStatus: "pending", memberOnboardingStatus: "pending" }, false, onboarding),
    ).toBe("/edition-hello");
  });

  it("does nothing for a pending member without a member route (Community)", () => {
    expect(onboardingRedirect({ memberOnboardingStatus: "pending" }, false, null)).toBeNull();
    expect(onboardingRedirect({ memberOnboardingStatus: "pending" }, false, { adminRoute: "/x" })).toBeNull();
  });

  it("waits while the caller or their rights are unknown", () => {
    expect(onboardingRedirect(undefined, true, onboarding)).toBeNull();
    expect(onboardingRedirect({ onboardingStatus: "pending", memberOnboardingStatus: "pending" }, null, onboarding)).toBeNull();
  });

  it("does nothing once both are finished", () => {
    expect(
      onboardingRedirect({ onboardingStatus: "skipped", memberOnboardingStatus: "completed" }, true, onboarding),
    ).toBeNull();
  });
});

describe("edition slots", () => {
  it("renders the sidebar header and footer with the collapsed state", () => {
    render(
      <EditionCompositionProvider
        extensions={[
          {
            id: "a",
            sidebarHeader: ({ collapsed }) => <div>header {String(collapsed)}</div>,
            sidebarFooter: ({ collapsed }) => <div>footer {String(collapsed)}</div>,
          },
        ]}
      >
        <EditionSidebarHeader collapsed={false} />
        <EditionSidebarFooter collapsed />
      </EditionCompositionProvider>,
    );
    expect(screen.getByText("header false")).toBeInTheDocument();
    expect(screen.getByText("footer true")).toBeInTheDocument();
  });

  it("renders profile sections in extension order", () => {
    render(
      <EditionCompositionProvider
        extensions={[
          { id: "a", profileSections: [{ id: "one", component: () => <section>First</section> }] },
          { id: "b", profileSections: [{ id: "two", component: () => <section>Second</section> }] },
        ]}
      >
        <EditionProfileSections />
      </EditionCompositionProvider>,
    );
    expect(screen.getAllByText(/First|Second/).map((n) => n.textContent)).toEqual(["First", "Second"]);
  });

  it("renders nothing without an edition", () => {
    const { container } = render(
      <>
        <EditionSidebarHeader collapsed={false} />
        <EditionSidebarFooter collapsed={false} />
        <EditionProfileSections />
      </>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
