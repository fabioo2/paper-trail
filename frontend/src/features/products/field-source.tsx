import { Badge } from "@/components/ui/badge";
import type { FieldSource } from "@/features/products/api";
import { cn } from "@/lib/utils";

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
    return <GuessBadge label="Guessed by Claude" />;
  }
  return (
    <span className="text-xs font-normal text-muted-foreground">
      {SOURCE_LABELS[source]}
    </span>
  );
}

/** The small "AI" marker for Claude's guesses, on a field or on a product that has some. */
export function GuessBadge({
  label = "Has details Claude guessed",
  className,
}: {
  label?: string;
  className?: string;
}) {
  return (
    <Badge
      variant="outline"
      role="img"
      aria-label={label}
      title={label}
      className={cn("border-warning px-1.5 py-0 text-[10px]", className)}
    >
      AI
    </Badge>
  );
}
