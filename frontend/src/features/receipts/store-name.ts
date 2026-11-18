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

/** Chains whose branches print more after the name, e.g. "Costco Wholesale Windsor #534";
 * the same list as the server's `KNOWN_CHAINS`. */
const CHAINS = new Set([
  "costco",
  "walmart",
  "metro",
  "loblaws",
  "real canadian superstore",
  "no frills",
  "food basics",
  "freshco",
  "sobeys",
  "zehrs",
  "iga",
  "farm boy",
  "giant tiger",
  "shoppers drug mart",
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
 * "metro" is "Metro". A name that starts with a known chain is that chain; otherwise store
 * numbers and format suffixes go. Every word is title-cased; the stored name is never changed.
 */
export function displayStoreName(name: string): string {
  const words = name.trim().split(/\s+/);
  for (let end = 1; end < words.length; end++) {
    if (CHAINS.has(words.slice(0, end).join(" ").toLowerCase())) {
      words.splice(end);
      break;
    }
  }
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
