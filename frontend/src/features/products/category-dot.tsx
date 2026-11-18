import type { Category } from "@/features/products/api";

/** The category's colour as a small dot beside its name (decorative: the name carries it). */
export function CategoryDot({ category }: { category: Category | null }) {
  if (category === null) return null;
  return (
    <span
      aria-hidden="true"
      data-category={category}
      className="inline-block size-2 shrink-0 rounded-full"
      style={{ backgroundColor: `var(--cat-${category.replaceAll("_", "-")})` }}
    />
  );
}
