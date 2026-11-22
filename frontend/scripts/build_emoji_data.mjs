// Regenerates src/picker/emoji/*.json (committed). Only needed for a new emoji version.
// The two data packages are NOT app dependencies (users would download ~110 MB);
// fetch them first, without saving:
//   npm install --no-save emojibase-data@18.0.0 cldr-annotations-full@49.0.0
//   node scripts/build_emoji_data.mjs
import { mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const languages = new URL("../src/lang/", import.meta.url);
const output = new URL("../src/picker/emoji/", import.meta.url);
const emojibase = (lang) => require(`emojibase-data/${lang}/data.json`);
const cldrLanguages = new Map([["ar", "ar"], ["id", "id"], ["ta", "ta"], ["pt-pt", "pt"]]);
// emojibase group 2 is made up of skin and hair components. Groups 0 and 1
// share a picker category. Entries without an order are regional indicators.
const ordered = emojibase("en")
  .filter(({ group, order }) => group !== 2 && Number.isFinite(order))
  .sort((a, b) => a.order - b.order);
const base = ordered.map(({ emoji, group, skins }) => skins?.length
  ? [emoji, group, skins.map((skin) => skin.emoji)]
  : [emoji, group]);

function annotations(lang) {
  const cldr = cldrLanguages.get(lang);
  if (cldr) {
    const { annotations: { annotations } } = require(`cldr-annotations-full/annotations/${cldr}/annotations.json`);
    return ordered.map(({ emoji }) => {
      const entry = annotations[emoji] ?? annotations[emoji.replaceAll("\uFE0F", "")];
      return entry?.tts?.[0] ? [entry.tts[0], ...(entry.default ?? [])] : null;
    });
  }
  const byCode = new Map(emojibase(lang).map((entry) => [entry.hexcode, entry]));
  return ordered.map(({ hexcode }) => {
    const entry = byCode.get(hexcode);
    return entry ? [entry.label, ...(entry.tags ?? [])] : null;
  });
}

function write(name, value) {
  writeFileSync(new URL(name, output), JSON.stringify(value) + "\n", "utf8");
}

mkdirSync(output, { recursive: true });
write("base.json", base);
const files = readdirSync(languages).filter((file) => file.endsWith(".json"));
files.forEach((file) => write(file, annotations(file.slice(0, -".json".length))));
console.log(`Wrote ${base.length} emoji and ${files.length} locale files.`);
