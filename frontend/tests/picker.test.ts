import assert from "node:assert/strict";
import {
  categoryAtOffset, EMOJI_CATEGORIES, emojiCategory, emojiVariants, indexEmoji, mergeRecents, searchEmoji,
} from "../src/picker/emojiData";
import type { EmojiRow } from "../src/picker/emojiData";
import { RECENT_LIMIT } from "../src/picker/tunables";
import base from "../src/picker/emoji/base.json";
import en from "../src/picker/emoji/en.json";
import ja from "../src/picker/emoji/ja.json";
import zh from "../src/picker/emoji/zh.json";

const emoji = indexEmoji(base as EmojiRow[], en);
const japanese = indexEmoji(base as EmojiRow[], en, ja);
const chinese = indexEmoji(base as EmojiRow[], en, zh);

assert.equal(searchEmoji(emoji, "cat")[0].emoji, "🐈");
assert.ok(searchEmoji(japanese, "猫").some((entry) => entry.emoji === "🐈" || entry.emoji === "🐱"));
assert.ok(searchEmoji(chinese, "猫").some((entry) => entry.emoji === "🐈" || entry.emoji === "🐱"));
assert.ok(searchEmoji(japanese, "cat").some((entry) => entry.emoji === "🐈"), "English stays searchable");
assert.ok(searchEmoji(chinese, "CAT").some((entry) => entry.emoji === "🐈"));
assert.ok(searchEmoji(emoji, "kitten").some((entry) => entry.emoji === "🐱"), "keywords are indexed");

const variants = emojiVariants(emoji);
assert.deepEqual(variants.get("👋")?.skins, ["👋🏻", "👋🏼", "👋🏽", "👋🏾", "👋🏿"]);
assert.equal(variants.get("👋🏽"), variants.get("👋"));
assert.equal(variants.get("🐈")?.skins.length, 0);

assert.deepEqual(mergeRecents(["👋🏽", "🐈", "👋🏽"], ["🐈", "👋", "🐱"], 3), ["👋🏽", "🐈", "👋"]);
assert.equal(mergeRecents([], emoji.map((entry) => entry.emoji)).length, RECENT_LIMIT);
assert.equal(EMOJI_CATEGORIES[0].id, "recent");
assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map(emojiCategory),
  ["people", "people", undefined, "nature", "food", "travel", "activities", "objects", "symbols", "flags"]);
const offsets = [{ id: "recent", top: 0 }, { id: "people", top: 72 }, { id: "nature", top: 542 }] as const;
assert.equal(categoryAtOffset([...offsets], 0), "recent");
assert.equal(categoryAtOffset([...offsets], 541), "people");
assert.equal(categoryAtOffset([...offsets], 542), "nature");

console.log("PASS: picker search, tone variants, recent order and category jumps");
