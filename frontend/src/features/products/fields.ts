import {
  type Category,
  type ProductOut,
  type ProductUpdateIn,
  sizeText,
} from "@/features/products/api";

/** A product's fields as a person types them. */
export interface Fields {
  canonical_name: string;
  brand: string;
  size: string;
  category: Category | "";
  tags: string;
}

export function fieldsOf(product: ProductOut): Fields {
  return {
    canonical_name: product.canonical_name,
    brand: product.brand ?? "",
    size: sizeText(product),
    category: product.category ?? "",
    tags: product.tags.join(", "),
  };
}

/** Only the fields the person changed, as the API takes them. */
export function changes(before: Fields, after: Fields): ProductUpdateIn {
  const body: ProductUpdateIn = {};
  if (after.canonical_name !== before.canonical_name)
    body.canonical_name = after.canonical_name;
  if (after.brand !== before.brand) body.brand = after.brand;
  if (after.size !== before.size) body.size = after.size;
  if (after.category !== before.category && after.category !== "")
    body.category = after.category;
  if (after.tags !== before.tags)
    body.tags = after.tags
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean);
  return body;
}
