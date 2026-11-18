/** Store-format and legal-form words printed after a chain's name, e.g. "Costco Wholesale". */
const SUFFIXES = new Set([
  "wholesale",
  "supercentre",
  "supercenter",
  "inc",
  "inc.",
  "ltd",
  "ltd.",
  "corp",
  "corp.",
]);

/** Names whose usual spelling title case can't produce. */
const SPELLINGS: Record<string, string> = {
  iga: "IGA",
  freshco: "FreshCo",
};

const STORE_NUMBER = /^(#\d+|\d+)$/;
const WORD = /\p{L}[\p{L}']*/gu;

function titleCase(word: string): string {
  return word.replace(
    WORD,
    (part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase(),
  );
}

/**
 * The store as people say it, for display only: "COSTCO WHOLESALE #1234" is "Costco",
 * "metro" is "Metro". Store numbers and format suffixes go and every word is title-cased;
 * the stored name is never changed.
 */
export function displayStoreName(name: string): string {
  const words = name.trim().split(/\s+/);
  while (words.length > 1) {
    const last = words.at(-1)?.toLowerCase() ?? "";
    if (STORE_NUMBER.test(last)) {
      words.pop();
      if (words.length > 1 && words.at(-1)?.toLowerCase() === "store") {
        words.pop();
      }
    } else if (SUFFIXES.has(last)) {
      words.pop();
    } else {
      break;
    }
  }
  return (
    SPELLINGS[words.join(" ").toLowerCase()] ?? words.map(titleCase).join(" ")
  );
}
