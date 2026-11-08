/**
 * jsdom implements no pointer capture. Sonner's toasts call `setPointerCapture` on pointerdown
 * (swipe to dismiss), so clicking a toast's action button would throw without these no-ops.
 */
export function installPointerCapture(): void {
  if ("setPointerCapture" in Element.prototype) return;
  Object.assign(Element.prototype, {
    setPointerCapture(): void {
      // nothing to capture without a real pointer
    },
    releasePointerCapture(): void {
      // nothing was captured
    },
    hasPointerCapture(): boolean {
      return false;
    },
  });
}
