import { useEffect, useMemo, useRef, useState } from "react";
import { availableLanguages } from "../i18n";
import {
  categoryAtOffset, EMOJI_CATEGORIES, emojiVariants, indexEmoji, searchEmoji,
} from "./emojiData";
import type { Annotations, CategoryId, EmojiEntry, EmojiRow } from "./emojiData";
import { useGridKeys, useLongPress } from "./interaction";
import { CategoryButton, PickerMenu, PickerSearch } from "./PickerControls";
import { readTones, saveTones } from "./preferences";
import { CATEGORY_SCROLL_TOLERANCE } from "./tunables";

const loadAnnotations = (lang: string): Promise<Annotations> =>
  import(`./emoji/${lang}.json`).then((module) => module.default);

function loadEmoji(lang: string) {
  const english = loadAnnotations("en");
  const local = lang === "en" ? english : loadAnnotations(lang).catch(() => english);
  return Promise.all([import("./emoji/base.json"), english, local])
    .then(([base, en, locale]) => indexEmoji(base.default as EmojiRow[], en, locale));
}

// Every language's names, loaded only when a search finds nothing in the page's
// language + English (someone typing 猫 on an English page). No per-language rules.
async function loadAllNames(entries: EmojiEntry[]) {
  const codes = [...new Set(["en", ...availableLanguages().map((info) => info.code)])];
  const base = (await import("./emoji/base.json")).default as EmojiRow[];
  const all: Annotations[] = await Promise.all(
    codes.map((code) => loadAnnotations(code).catch(() => [] as Annotations)));
  const extra = new Map<string, string>();
  base.forEach(([emoji], i) => {
    const words = all.flatMap((names) => names[i] ?? []);
    if (words.length) extra.set(emoji, words.join("\n"));
  });
  return entries.map((entry) => {
    const more = extra.get(entry.emoji);
    return more ? { ...entry, search: `${entry.search}\n${indexEmoji([[entry.emoji, 0]], [[more]])[0].search}` } : entry;
  });
}

export function EmojiTab({ lang, recent, onPick }: {
  lang: string; recent: string[]; onPick: (emoji: string) => void;
}) {
  const [entries, setEntries] = useState<EmojiEntry[]>([]);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<CategoryId>("recent");
  const [tones, setTones] = useState(readTones);
  const [variants, setVariants] = useState<{ entry: EmojiEntry; button: HTMLElement } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const sections = useRef(new Map<CategoryId, HTMLElement>());
  const jumpTo = useRef<CategoryId | null>(null);
  const press = useLongPress();
  const grid = useGridKeys();

  useEffect(() => {
    let active = true;
    setFailed(false);
    void loadEmoji(lang).then((items) => { if (active) setEntries(items); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [lang]);

  const byVariant = useMemo(() => emojiVariants(entries), [entries]);
  const groups = useMemo(() => {
    const result = new Map<CategoryId, EmojiEntry[]>();
    entries.forEach((entry) => {
      const group = result.get(entry.category) ?? [];
      group.push(entry);
      result.set(entry.category, group);
    });
    return result;
  }, [entries]);
  const searching = Boolean(query.trim());
  const [allNames, setAllNames] = useState<EmojiEntry[] | null>(null);
  const local = useMemo(() => searching ? searchEmoji(entries, query) : [], [entries, query, searching]);
  const needAll = searching && entries.length > 0 && local.length === 0;
  useEffect(() => {
    if (!needAll || allNames) return;
    let active = true;
    void loadAllNames(entries).then((items) => { if (active) setAllNames(items); }).catch(() => {});
    return () => { active = false; };
  }, [needAll, allNames, entries]);
  useEffect(() => { setAllNames(null); }, [entries]);
  const matches = useMemo(
    () => (needAll && allNames ? searchEmoji(allNames, query) : local),
    [needAll, allNames, query, local],
  );

  function setSearch(value: string) {
    setQuery(value);
    setCategory("recent");
    if (scroller.current) scroller.current.scrollTop = 0;
  }

  function moveTo(id: CategoryId) {
    setCategory(id);
    if (searching) {
      jumpTo.current = id;
      setQuery("");
    } else {
      scrollTo(id);
    }
  }

  function scrollTo(id: CategoryId) {
    const box = scroller.current;
    const section = sections.current.get(id);
    if (box && section) box.scrollTop += section.getBoundingClientRect().top - box.getBoundingClientRect().top;
  }

  useEffect(() => {
    const target = jumpTo.current;
    if (target) { scrollTo(target); jumpTo.current = null; }
  }, [searching]);

  function followScroll() {
    press.cancel();
    const box = scroller.current;
    if (!box || searching) return;
    const top = box.getBoundingClientRect().top;
    const offsets = EMOJI_CATEGORIES.flatMap(({ id }) => {
      const section = sections.current.get(id);
      return section ? [{ id, top: section.getBoundingClientRect().top - top }] : [];
    });
    setCategory(categoryAtOffset(offsets, CATEGORY_SCROLL_TOLERANCE));
  }

  function closeVariants() {
    variants?.button.focus({ preventScroll: true });
    setVariants(null);
  }

  function pickTone(entry: EmojiEntry, emoji: string) {
    const next = { ...tones, [entry.emoji]: emoji };
    setTones(next);
    saveTones(next);
    closeVariants();
    onPick(emoji);
  }

  function cell(emoji: string, entry: EmojiEntry | undefined, tabStop: boolean, recent = false) {
    const stored = entry && tones[entry.emoji];
    const glyph = !recent && stored && entry?.skins.includes(stored) ? stored : emoji;
    const toneActions = entry?.skins.length ? press.bind((button) => setVariants({ entry, button })) : {};
    return (
      <button type="button" key={emoji} data-picker-cell className="picker-emoji-cell picker-emoji-font"
        tabIndex={tabStop ? 0 : -1} title={entry?.label ?? glyph} aria-label={entry?.label ?? glyph}
        aria-keyshortcuts={entry?.skins.length ? "shift+f10" : undefined} {...toneActions}
        onClick={(event) => { if (!press.consume(event.currentTarget, event.detail)) onPick(glyph); }}>
        {glyph}
      </button>
    );
  }

  return (
    <>
      <div className="picker-categories" role="group" aria-label="Emoji categories">
        {EMOJI_CATEGORIES.map(({ id, label, icon }) => (
          <CategoryButton key={id} label={label} icon={icon} active={!searching && category === id} onClick={() => moveTo(id)} />
        ))}
      </div>
      <PickerSearch label="Search emoji" value={query} onChange={setSearch} />
      <div className="picker-scroll" ref={scroller} onScroll={followScroll} {...grid}>
        {failed ? <p role="status">Could not load emoji.</p> : !entries.length ? <p role="status">Loading…</p>
          : searching ? (
            <div key="search" className="picker-grid picker-emoji-grid">
              {matches.length ? matches.map((entry, i) => cell(entry.emoji, entry, i === 0))
                : <p role="status">No emoji found.</p>}
            </div>
          ) : EMOJI_CATEGORIES.map(({ id, label }) => (
            <section key={id} className="picker-emoji-section" ref={(element) => {
              if (element) sections.current.set(id, element);
              else sections.current.delete(id);
            }}>
              <h3>{label}</h3>
              <div className="picker-grid picker-emoji-grid">
                {id === "recent"
                  ? recent.map((emoji, i) => cell(emoji, byVariant.get(emoji), i === 0, true))
                  : groups.get(id)?.map((entry, i) => cell(entry.emoji, entry, !recent.length && id === "people" && i === 0))}
              </div>
              {id === "recent" && !recent.length && <p>No recent emoji.</p>}
            </section>
          ))}
      </div>
      {variants && (
        <PickerMenu label="Skin tone" close={closeVariants}>
          <div className="picker-grid picker-emoji-grid" {...grid}>
            {[variants.entry.emoji, ...variants.entry.skins].map((emoji, i) => (
              <button key={emoji} type="button" className="picker-emoji-cell picker-emoji-font"
                data-picker-cell tabIndex={i === 0 ? 0 : -1} aria-label={emoji}
                onClick={() => pickTone(variants.entry, emoji)}>{emoji}</button>
            ))}
          </div>
        </PickerMenu>
      )}
    </>
  );
}
