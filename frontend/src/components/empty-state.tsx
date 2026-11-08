import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description: string;
  /** Optional call to action below the description. */
  children?: ReactNode;
}

/** A centred icon, heading and one-line explanation for a screen with nothing to show yet. */
export function EmptyState({
  icon: Icon,
  title,
  description,
  children,
}: EmptyStateProps) {
  return (
    <section className="mx-auto flex max-w-sm flex-col items-center gap-3 px-4 py-16 text-center">
      <div className="flex size-14 items-center justify-center rounded-full bg-accent text-accent-foreground">
        <Icon aria-hidden="true" className="size-7" />
      </div>
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      <p className="text-sm text-muted-foreground">{description}</p>
      {children}
    </section>
  );
}
