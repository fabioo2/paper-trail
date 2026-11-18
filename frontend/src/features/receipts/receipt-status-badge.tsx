import { Badge } from "@/components/ui/badge";
import type { ReceiptStatus } from "@/features/receipts/api";

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

const PRESENTATION: Record<
  Exclude<ReceiptStatus, "completed">,
  { label: string; variant: BadgeVariant; className?: string }
> = {
  processing: { label: "Reading…", variant: "secondary" },
  needs_review: {
    label: "Needs a look",
    variant: "outline",
    className: "border-warning",
  },
  failed: { label: "Couldn't read", variant: "destructive" },
};

/** A label for a receipt that isn't done yet, e.g. "Needs a look"; nothing once completed. */
export function ReceiptStatusBadge({ status }: { status: ReceiptStatus }) {
  if (status === "completed") return null;
  const { label, variant, className } = PRESENTATION[status];
  return (
    <Badge variant={variant} className={className}>
      {label}
    </Badge>
  );
}
