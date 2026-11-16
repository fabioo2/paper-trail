interface NodeProcess {
  getBuiltinModule(id: "node:buffer"): { File: typeof File };
}

/**
 * Vitest's jsdom environment swaps in jsdom's FormData and File, but requests still go through
 * Node's fetch, which only serialises its own classes as multipart (jsdom's FormData becomes
 * the text "[object FormData]", a jsdom File "[object File]"). Put Node's back so uploads reach
 * MSW as real multipart requests with the file's name, type and bytes. Node exposes no
 * FormData module, so its class is recovered from a parsed form body.
 */
export async function installNodeFormData(): Promise<void> {
  const parsed = await new Response("probe=1", {
    headers: { "content-type": "application/x-www-form-urlencoded" },
  }).formData();
  const { process } = globalThis as unknown as { process: NodeProcess };
  globalThis.FormData = parsed.constructor as typeof FormData;
  globalThis.File = process.getBuiltinModule("node:buffer").File;
}
