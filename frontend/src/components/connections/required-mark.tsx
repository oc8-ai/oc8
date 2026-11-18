/** The "*" after a required field's label, drawn by CSS so the field's
 *  accessible name stays its label (the input carries `required`). */
export function RequiredMark({ className = "" }: { className?: string }) {
  return <span aria-hidden data-required-mark className={`after:content-['*'] ${className}`} />;
}
