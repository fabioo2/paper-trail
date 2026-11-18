import { useNavigate } from "@tanstack/react-router";
import { type ReactNode, type SyntheticEvent, useId, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  type Category,
  type FieldSource,
  type ProductOut,
  type ProductUpdateIn,
  isStale,
  STALE_MESSAGE,
  shownValues,
  sizeText,
  useCategories,
  useConfirmProduct,
  useUpdateProduct,
} from "@/features/products/api";
import { FieldSourceMark } from "@/features/products/field-source";

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

/** One product's fields, for a person to correct or accept; both are refused (409) when the
 * product changed since the editor opened. */
export function ProductEditor({ product }: { product: ProductOut }) {
  const [initial] = useState(() => fieldsOf(product));
  const [shown] = useState(() => shownValues(product));
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
    const expected = Object.fromEntries(
      Object.entries(shown).filter(([key]) => key in body),
    );
    update.mutate(
      { ...body, expected },
      {
        onSuccess: () => {
          done("Product saved");
        },
      },
    );
  };
  const accept = () => {
    update.reset();
    confirm.mutate(
      { expected: shown },
      {
        onSuccess: () => {
          done("Product confirmed");
        },
      },
    );
  };
  const set = (key: keyof Fields) => (event: { target: { value: string } }) => {
    setFields((current) => ({ ...current, [key]: event.target.value }));
  };

  return (
    <form className="flex flex-col gap-4" onSubmit={save}>
      <Field label="Name" source={product.field_sources.canonical_name}>
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
      <Field label="Brand" source={product.field_sources.brand}>
        {(id) => (
          <Input
            id={id}
            maxLength={120}
            value={fields.brand}
            onChange={set("brand")}
          />
        )}
      </Field>
      <Field label="Size" source={product.field_sources.size}>
        {(id) => (
          <Input
            id={id}
            placeholder="500 g"
            value={fields.size}
            onChange={set("size")}
          />
        )}
      </Field>
      <Field label="Category" source={product.field_sources.category}>
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
      <Field label="Tags" source={product.field_sources.tags}>
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
          {isStale(error) ? STALE_MESSAGE : error.message}
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
  );
}

function Field({
  label,
  source,
  children,
}: {
  label: string;
  source: FieldSource | undefined;
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
        <FieldSourceMark source={source} />
      </label>
      {children(id)}
    </div>
  );
}
