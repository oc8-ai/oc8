import type { EditionSidebarSlotProps } from "./contracts";
import { useFrontendEdition } from "./composition";

/** The edition's sidebar header, if one supplies it. */
export function EditionSidebarHeader({ collapsed }: EditionSidebarSlotProps) {
  const { sidebarHeader: Header } = useFrontendEdition();
  return Header ? <Header collapsed={collapsed} /> : null;
}

/** The edition's sidebar footer, if one supplies it. */
export function EditionSidebarFooter({ collapsed }: EditionSidebarSlotProps) {
  const { sidebarFooter: Footer } = useFrontendEdition();
  return Footer ? <Footer collapsed={collapsed} /> : null;
}

/** Panels editions append to the profile page, in extension order. */
export function EditionProfileSections() {
  const { profileSections } = useFrontendEdition();
  return (
    <>
      {profileSections.map(({ id, component: Section }) => (
        <Section key={id} />
      ))}
    </>
  );
}
