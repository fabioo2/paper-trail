import { Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, SearchX, TriangleAlert } from "lucide-react";
import { type ReactNode, type SyntheticEvent, useId, useState } from "react";
import { toast } from "sonner";

import { ApiError } from "@/api/client";
import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type Category,
  type ProductOut,
  type ProductUpdateIn,
  sizeText,
  useCategories,
  useConfirmProduct,
  useProduct,
  useUpdateProduct,
} from "@/features/products/api";

/** One product's fields, for a person to correct or accept. */
export function ProductEditor({ productId }: { productId: number }) {
  const product = useProduct(productId);

  if (product.isPending) return <ProductEditorSkeleton />;
  if (product.isError) {
    const notFound =
      product.error instanceof ApiError && product.error.status === 404;
    return notFound ? (
      <ProductNotFound />
    ) : (
      <EmptyState
        icon={TriangleAlert}
        title="Couldn't open this product"
        description="Paper Trail could not reach the server. Pull to refresh or try again later."
      />
    );
  }
  return <ProductForm key={product.data.id} product={product.data} />;
}

export function ProductNotFound() {
  return (
    <EmptyState
      icon={SearchX}
      title="Product not found"
      description="This product does not exist. It may have been merged, or the link is wrong."
    />
  );
}

interface Fields {
  canonical_name: string;
  brand: string;
  size: string;
  category: Category | "";
  tags: string;
}

function fieldsOf(product: ProductOut): Fields {
  return {
    canonical_name: product.canonical_name,
    brand: product.brand ?? "",
    size: sizeText(product),
    category: product.category ?? "",
    tags: product.tags.join(", "),
  };
}

/** Only the fields the person changed, as the API takes them. */
function changes(before: Fields, after: Fields): ProductUpdateIn {
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

function ProductForm({ product }: { product: ProductOut }) {
  const [initial] = useState(() => fieldsOf(product));
  const [fields, setFields] = useState(initial);
  const categories = useCategories();
  const update = useUpdateProduct(product.id);
  const confirm = useConfirmProduct(product.id);
  const navigate = useNavigate();
  const busy = update.isPending || confirm.isPending;
  const body = changes(initial, fields);
  const error = update.error ?? confirm.error;

  const done = (message: string) => {
    toast.success(message);
    void navigate({ to: "/products" });
  };
  const save = (event: SyntheticEvent) => {
    event.preventDefault();
    confirm.reset();
    update.mutate(body, {
      onSuccess: () => {
        done("Product saved");
      },
    });
  };
  const accept = () => {
    update.reset();
    confirm.mutate(undefined, {
      onSuccess: () => {
        done("Product confirmed");
      },
    });
  };
  const set = (key: keyof Fields) => (event: { target: { value: string } }) => {
    setFields((current) => ({ ...current, [key]: event.target.value }));
  };
  const suggested = (key: string) => product.field_sources[key] === "llm";

  return (
    <article className="flex flex-col gap-4 px-4 py-4">
      <Link
        to="/products"
        className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        Products
      </Link>
      <h1 className="text-xl font-semibold tracking-tight break-words">
        {product.canonical_name}
      </h1>
      <form className="flex flex-col gap-4" onSubmit={save}>
        <Field label="Name" suggested={suggested("canonical_name")}>
          {(id) => (
            <Input
              id={id}
              required
              maxLength={120}
              value={fields.canonical_name}
              onChange={set("canonical_name")}
            />
          )}
        </Field>
        <Field label="Brand" suggested={suggested("brand")}>
          {(id) => (
            <Input
              id={id}
              maxLength={120}
              value={fields.brand}
              onChange={set("brand")}
            />
          )}
        </Field>
        <Field label="Size" suggested={suggested("size")}>
          {(id) => (
            <Input
              id={id}
              placeholder="500 g"
              value={fields.size}
              onChange={set("size")}
            />
          )}
        </Field>
        <Field label="Category" suggested={suggested("category")}>
          {(id) => (
            <select
              id={id}
              value={fields.category}
              onChange={set("category")}
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-base shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 md:text-sm dark:bg-input/30"
            >
              {fields.category === "" ? (
                <option value="" disabled>
                  Not set
                </option>
              ) : null}
              {(categories.data ?? []).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Tags" suggested={suggested("tags")}>
          {(id) => (
            <Input
              id={id}
              placeholder="organic, free-run"
              value={fields.tags}
              onChange={set("tags")}
            />
          )}
        </Field>
        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error.message}
          </p>
        ) : null}
        <div className="flex flex-col gap-2 sm:flex-row-reverse">
          <Button
            type="submit"
            size="lg"
            disabled={busy || Object.keys(body).length === 0}
          >
            Save
          </Button>
          {product.confirmed ? null : (
            <Button
              type="button"
              size="lg"
              variant="outline"
              disabled={busy || Object.keys(body).length > 0}
              onClick={accept}
            >
              Looks right
            </Button>
          )}
        </div>
      </form>
    </article>
  );
}

function Field({
  label,
  suggested,
  children,
}: {
  label: string;
  suggested: boolean;
  children: (id: string) => ReactNode;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <label
        htmlFor={id}
        className="flex items-center gap-2 text-sm font-medium"
      >
        {label}
        {suggested ? (
          <Badge variant="outline" className="border-warning">
            Suggested
          </Badge>
        ) : null}
      </label>
      {children(id)}
    </div>
  );
}

function ProductEditorSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading product"
      className="flex flex-col gap-4 px-4 py-4"
    >
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-9 w-full" />
      <Skeleton className="h-9 w-full" />
      <Skeleton className="h-9 w-full" />
    </div>
  );
}
