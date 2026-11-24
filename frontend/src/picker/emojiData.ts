import { RECENT_LIMIT } from "./tunables";

export const EMOJI_CATEGORIES = [
  { id: "recent", label: "Recent", icon: "◷" },
  { id: "people", label: "Smileys & People", icon: "😀" },
  { id: "nature", label: "Animals & Nature", icon: "🐻" },
  { id: "food", label: "Food & Drink", icon: "🍕" },
  { id: "travel", label: "Travel & Places", icon: "🚗" },
  { id: "activities", label: "Activities", icon: "⚽" },
  { id: "objects", label: "Objects", icon: "💡" },
  { id: "symbols", label: "Symbols", icon: "❤️" },
  { id: "flags", label: "Flags", icon: "🏁" },
] as const;

export type CategoryId = typeof EMOJI_CATEGORIES[number]["id"];
export type EmojiRow = [emoji: string, group: number, skins?: string[]];
export type Annotations = (string[] | null)[];
export type EmojiEntry = { emoji: string; category: CategoryId; label: string; search: string; skins: string[] };

const groups: Record<number, CategoryId> = {
  0: "people", 1: "people", 3: "nature", 4: "food", 5: "travel",
  6: "activities", 7: "objects", 8: "symbols", 9: "flags",
};

export function emojiCategory(group: number): CategoryId | undefined {
  return groups[group];
}

function fold(text: string) {
  return text.normalize("NFKD").replace(/(\p{Script=Latin})\p{M}+/gu, "$1").toLowerCase().normalize("NFC");
}

export function indexEmoji(base: EmojiRow[], english: Annotations, local: Annotations = english) {
  return base.flatMap(([emoji, group, skins = []], i): EmojiEntry[] => {
    const category = emojiCategory(group);
    if (!category) return [];
    const label = local[i]?.[0] ?? english[i]?.[0] ?? emoji;
    const search = fold([emoji, ...skins, ...new Set([...(local[i] ?? []), ...(english[i] ?? [])])].join("\n"));
    return [{ emoji, category, label, search, skins }];
  });
}

export function searchEmoji(entries: EmojiEntry[], query: string) {
  const needle = fold(query).trim();
  if (!needle) return entries;
  const words = needle.split(/\s+/);
  return entries.flatMap((entry) => {
    if (!words.every((word) => entry.search.includes(word))) return [];
    const label = fold(entry.label);
    const rank = label === needle ? 0 : entry.search.split("\n").includes(needle) ? 1 : label.startsWith(needle) ? 2 : 3;
    return [{ entry, rank }];
  }).sort((a, b) => a.rank - b.rank).map(({ entry }) => entry);
}

export function emojiVariants(entries: EmojiEntry[]) {
  return new Map(entries.flatMap((entry) => [entry.emoji, ...entry.skins].map((emoji) => [emoji, entry] as const)));
}

export function mergeRecents(newest: string[], older: string[], cap = RECENT_LIMIT) {
  return [...new Set([...newest, ...older])].slice(0, cap);
}

export function categoryAtOffset(offsets: { id: CategoryId; top: number }[], scrollTop: number): CategoryId {
  return offsets.filter(({ top }) => top <= scrollTop).at(-1)?.id ?? "recent";
}
