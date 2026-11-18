import { Badge } from "@/components/ui/badge";
import type { FieldSource } from "@/features/products/api";

const SOURCE_LABELS: Record<Exclude<FieldSource, "llm">, string> = {
  receipt_text: "printed",
  person: "you",
  plu: "PLU",
  off: "barcode",
  upc: "barcode",
  legacy_model: "older guess",
};

/** Where a field came from: an "AI" marker for Claude's guess, muted text for the rest. */
export function FieldSourceMark({
  source,
}: {
  source: FieldSource | undefined;
}) {
  if (source === undefined) return null;
  if (source === "llm") {
    return (
      <Badge
        variant="outline"
        className="border-warning px-1.5 py-0 text-[10px]"
        title="Guessed by Claude"
      >
        AI
      </Badge>
    );
  }
  return (
    <span className="text-xs font-normal text-muted-foreground">
      {SOURCE_LABELS[source]}
    </span>
  );
}

/** A small dot on a product with fields Claude guessed and nobody has checked. */
export function GuessDot() {
  return (
    <span
      role="img"
      aria-label="Has AI guesses"
      title="Has AI guesses"
      className="inline-block size-2 shrink-0 rounded-full bg-warning"
    />
  );
}
