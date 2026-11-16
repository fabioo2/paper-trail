import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, unwrap } from "@/api/client";
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type ProductOut = Schemas["ProductOut"];
export type ProductSummaryOut = Schemas["ProductSummaryOut"];
export type ProductUpdateIn = Schemas["ProductUpdateIn"];
export type Category = Schemas["Category"];
export type CategoryOut = Schemas["CategoryOut"];

export const productKeys = {
  all: ["products"] as const,
  list: () => [...productKeys.all, "list"] as const,
  detail: (id: number) => [...productKeys.all, "detail", id] as const,
};

/** Every product bought at least once, by name (`listProducts`). */
export function useProductList() {
  return useQuery({
    queryKey: productKeys.list(),
    queryFn: () => unwrap(apiClient.GET("/api/products")),
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
    mutationFn: (body: ProductUpdateIn) =>
      unwrap(
        apiClient.PATCH("/api/products/{product_id}", {
          params: { path: { product_id: id } },
          body,
        }),
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: productKeys.all }),
  });
}

/** Accept the product's guessed values as the person's (`confirmProduct`). */
export function useConfirmProduct(id: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      unwrap(
        apiClient.POST("/api/products/{product_id}/confirm", {
          params: { path: { product_id: id } },
        }),
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: productKeys.all }),
  });
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
