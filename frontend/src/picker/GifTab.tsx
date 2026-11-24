import { useRef, useState } from "react";
import { selectionKey } from "./api";
import type { PickerState, Selection } from "./api";
import { Attribution, MediaGrid } from "./MediaGrid";
import { CategoryButton, PickerSearch } from "./PickerControls";
import { useDebouncedQuery, useGridKeys } from "./interaction";
import { useDiscovery, useProviderCategories } from "./useDiscovery";
import { ONLINE_QUERY_LIMIT } from "./tunables";

type Category = { id: string; query: string };

export type MediaTabProps = {
  state: PickerState; enabled: boolean; lang: string; blocked: boolean;
  onPick: (item: Selection) => void; onFavourite: (item: Selection) => void;
};

export const ONLINE_OFF = "Online search is off. Turn it on in Settings → Preferences.";
export const ONLINE_UNAVAILABLE = "Online search is unavailable.";

export function GifTab({ state, enabled, lang, blocked, onPick, onFavourite }: MediaTabProps) {
  const [category, setCategory] = useState<Category>({ id: "trending", query: "" });
  const [query, setQuery] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const grid = useGridKeys();
  const trimmed = query.trim();
  const debounced = useDebouncedQuery(trimmed);
  const categories = useProviderCategories(enabled, lang);
  const browsing = trimmed ? null : category.id === "recent" || category.id === "favourites" ? category.id : null;
  const discovery = useDiscovery("gif", lang, trimmed ? debounced : category.query,
    enabled && !browsing && (!trimmed || trimmed === debounced));
  const items = browsing ? state[browsing].gif : discovery.result?.items ?? [];
  const waiting = enabled && !browsing && (trimmed !== debounced || discovery.loading);
  const favourites = new Set(state.favourites.gif.map(selectionKey));
  const disabledOnline = !enabled || discovery.result?.status === "disabled";
  const offline = discovery.result?.status === "unreachable";
  const attribution = disabledOnline ? [] : [...new Set([...(categories?.attribution ?? []),
    ...(discovery.result?.attribution ?? [])])];

  function resetScroll() {
    if (scroller.current) scroller.current.scrollTop = 0;
  }

  function select(id: string, query = "") {
    setQuery("");
    setCategory({ id, query });
    resetScroll();
  }

  return (
    <>
      <div className="picker-categories" role="group" aria-label="GIF categories">
        <CategoryButton label="Trending" icon="↗" active={!trimmed && category.id === "trending"} onClick={() => select("trending")} />
        <CategoryButton label="Recent" icon="◷" active={browsing === "recent"} onClick={() => select("recent")} />
        <CategoryButton label="Favourites" icon="★" active={browsing === "favourites"} onClick={() => select("favourites")} />
        {categories?.items.map((item) => {
          const id = JSON.stringify([item.provider, item.query]);
          return (
            <button key={id} type="button" className="picker-category picker-category-word"
              aria-label={item.name} title={item.name} aria-pressed={!trimmed && category.id === id}
              onClick={() => select(id, item.query)}>
              <span aria-hidden="true">#</span> <span translate="no">{item.name}</span>
            </button>
          );
        })}
      </div>
      <PickerSearch label="Search GIFs" value={query} maxLength={ONLINE_QUERY_LIMIT}
        onChange={(value) => { setQuery(value); resetScroll(); }} />
      <div ref={scroller} key={`${browsing || category.id}:${trimmed}`} className="picker-scroll" {...grid}>
        {disabledOnline && <p role="status">{ONLINE_OFF}</p>}
        {offline && <p role="status">{ONLINE_UNAVAILABLE}</p>}
        <MediaGrid kind="gif" items={items} favourites={favourites} blocked={blocked} onPick={onPick} onFavourite={onFavourite} />
        {waiting ? <p role="status">Loading…</p> : !items.length && (browsing || (!disabledOnline && !offline)) && (
          <p role="status">{browsing === "recent" ? "No recent GIFs." :
            browsing === "favourites" ? "No favourite GIFs." : "No GIFs found."}</p>
        )}
        {discovery.result?.has_more && !waiting && !disabledOnline && (
          <button type="button" className="picker-more" onClick={discovery.more}>Load more</button>
        )}
      </div>
      <Attribution lines={attribution} />
    </>
  );
}
