/** Visible text of a DOM node (Node.textContent is nullable for documents and doctypes). */
export function textOf(node: Node): string {
  return (node.textContent ?? "").replace(/\s+/g, " ").trim();
}
