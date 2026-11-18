import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, apiClient, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { searchKeys } from "@/features/search/api";

type Schemas = components["schemas"];
export type ProductOut = Schemas["ProductOut"];
export type ProductSummaryOut = Schemas["ProductSummaryOut"];
export type ProductPurchaseOut = Schemas["ProductPurchaseOut"];
export type ProductUpdateIn = Schemas["ProductUpdateIn"];
export type ProductToCheckOut = Schemas["ProductToCheckOut"];
export type FieldSource = Schemas["FieldSource"];
export type FieldSourceKey = Schemas["FieldSourceKey"];
export type ProductConfirmIn = Schemas["ProductConfirmIn"];
export type Category = Schemas["Category"];
export type CategoryOut = Schemas["CategoryOut"];

export const productKeys = {
  all: ["products"] as const,
  list: () => [...productKeys.all, "list"] as const,
  toCheck: () => [...productKeys.all, "to-check"] as const,
  detail: (id: number) => [...productKeys.all, "detail", id] as const,
  purchases: (id: number) => [...productKeys.all, "purchases", id] as const,
};

/** Every product bought at least once, by name (`listProducts`). */
export function useProductList() {
  return useQuery({
    queryKey: productKeys.list(),
    queryFn: () => unwrap(apiClient.GET("/api/products")),
  });
}

/** The bought products nobody has confirmed, most bought first (`listProductsToCheck`); read
 * again on every mount, since enrichment adds to it in the background. */
export function useProductsToCheck() {
  return useQuery({
    queryKey: productKeys.toCheck(),
    queryFn: () => unwrap(apiClient.GET("/api/products/to-check")),
    refetchOnMount: "always",
  });
}

/** One product with its field sources (`getProduct`). */
export function useProduct(id: number) {
  return useQuery({
    queryKey: productKeys.detail(id),
    queryFn: () =>
      unwrap(
        apiClient.GET("/api/products/{product_id}", {
          params: { path: { product_id: id } },
        }),
      ),
  });
}

/** The product's purchases, newest first (`listProductPurchases`). */
export function useProductPurchases(id: number) {
  return useQuery({
    queryKey: productKeys.purchases(id),
    queryFn: () =>
      unwrap(
        apiClient.GET("/api/products/{product_id}/purchases", {
          params: { path: { product_id: id } },
        }),
      ),
  });
}

/** Every category with its label (`listCategories`); static, so never refetched. */
export function useCategories() {
  return useQuery({
    queryKey: ["categories"],
    queryFn: () => unwrap(apiClient.GET("/api/categories")),
    staleTime: Infinity,
  });
}

/** Write the fields a person changed (`updateProduct`). */
export function useUpdateProduct(id: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: productKeys.detail(id),
    mutationFn: (body: ProductUpdateIn) =>
      unwrap(
        apiClient.PATCH("/api/products/{product_id}", {
          params: { path: { product_id: id } },
          body,
        }),
      ),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: productKeys.all }),
        queryClient.invalidateQueries({ queryKey: searchKeys.all }),
      ]),
  });
}

/** Accept the product's values as the person's: every filled field, or only `fields`, refused
 * (409) when they differ from `expected` (`confirmProduct`). */
export function useConfirmProduct(id: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: productKeys.detail(id),
    mutationFn: (body: ProductConfirmIn) =>
      unwrap(
        apiClient.POST("/api/products/{product_id}/confirm", {
          params: { path: { product_id: id } },
          body,
        }),
      ),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: productKeys.all }),
        queryClient.invalidateQueries({ queryKey: searchKeys.all }),
      ]),
    onError: (error) =>
      error instanceof ApiError && error.status === 409
        ? queryClient.invalidateQueries({ queryKey: productKeys.all })
        : undefined,
  });
}

export const STALE_MESSAGE =
  "This product changed while you were looking. Check the new values and try again.";

/** Whether a confirmation was refused because the product changed since it was shown. */
export function isStale(error: Error | null): boolean {
  return error instanceof ApiError && error.status === 409;
}

/** The fields as the person sees them, for `confirmProduct`'s `expected`. */
export function shownValues(
  product: ProductOut,
  fields: readonly FieldSourceKey[] = [
    "canonical_name",
    "brand",
    "size",
    "category",
    "tags",
  ],
): NonNullable<ProductConfirmIn["expected"]> {
  const shown = {
    canonical_name: product.canonical_name,
    brand: product.brand,
    size: sizeText(product) || null,
    category: product.category,
    tags: product.tags.length > 0 ? product.tags : null,
  };
  return Object.fromEntries(fields.map((field) => [field, shown[field]]));
}

/** Whether any of the product's fields is Claude's guess. */
export function hasSuggestion(product: ProductOut): boolean {
  return Object.values(product.field_sources).includes("llm");
}

/** The size as a person types it, e.g. `500 g`; empty when unknown. */
export function sizeText(product: ProductOut): string {
  return product.size_value === null || product.size_unit === null
    ? ""
    : `${product.size_value} ${product.size_unit}`;
}
