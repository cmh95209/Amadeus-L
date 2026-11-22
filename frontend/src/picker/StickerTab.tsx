import { useEffect, useRef, useState } from "react";
import { deletePack, deleteSticker, getPacks, ownSticker, renamePack, reorderPacks, selectionKey } from "./api";
import type { Pack, Selection } from "./api";
import type { MediaTabProps } from "./GifTab";
import { ONLINE_UNAVAILABLE } from "./GifTab";
import { useDebouncedQuery, useGridKeys } from "./interaction";
import { Attribution, MediaGrid } from "./MediaGrid";
import { PackImport } from "./PackImport";
import { CategoryButton, PickerMenu, PickerSearch } from "./PickerControls";
import { ONLINE_QUERY_LIMIT } from "./tunables";
import { useDiscovery } from "./useDiscovery";

type Menu = { kind: "add" | "pack"; button: HTMLElement }
  | { kind: "sticker"; item: Selection; button: HTMLElement };

const ownItems = (packs: Pack[]) => packs.flatMap((pack) =>
  pack.stickers.map((sticker) => ownSticker(pack, sticker)));

export function StickerTab({ state, enabled, lang, blocked, onPick, onFavourite, refresh }: MediaTabProps & {
  refresh: () => void;
}) {
  const [packs, setPacks] = useState<Pack[]>([]);
  const [category, setCategory] = useState("recent");
  const [menu, setMenu] = useState<Menu | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<{ query: string; packs: Pack[] }>();
  const [revision, setRevision] = useState(0);
  const grid = useGridKeys();
  const scroller = useRef<HTMLDivElement>(null);
  const trimmed = query.trim();
  const debounced = useDebouncedQuery(trimmed);
  const searching = Boolean(trimmed);
  const searchReady = searching && trimmed === debounced;
  const discovery = useDiscovery("sticker", lang, debounced, enabled && searchReady);
  const pack = packs.find(({ id }) => id === category);
  const favourites = new Set(state.favourites.sticker.map(selectionKey));

  useEffect(() => {
    const controller = new AbortController();
    void getPacks("", controller.signal).then((result) => {
      if (!controller.signal.aborted) setPacks(result.packs);
    }).catch(() => {
      if (!controller.signal.aborted) setProblem("Could not load sticker packs.");
    });
    return () => controller.abort();
  }, [revision]);

  useEffect(() => {
    if (!searchReady) return;
    const controller = new AbortController();
    void getPacks(debounced, controller.signal).then((result) => {
      if (!controller.signal.aborted) setMatches({ query: debounced, packs: result.packs });
    }).catch(() => {
      if (!controller.signal.aborted) {
        setMatches({ query: debounced, packs: [] });
        setProblem("Could not load sticker packs.");
      }
    });
    return () => controller.abort();
  }, [debounced, revision, searchReady]);

  const ownResults = searching
    ? ownItems(matches?.query === trimmed ? matches.packs : [])
    : category === "recent" ? state.recent.sticker
      : category === "favourites" ? state.favourites.sticker : ownItems(pack ? [pack] : []);
  const online = discovery.result?.items ?? [];
  const waiting = searching && (matches?.query !== trimmed || discovery.loading);

  function select(id: string) {
    setCategory(id);
    setQuery("");
    setProblem("");
    if (scroller.current) scroller.current.scrollTop = 0;
  }

  function closeMenu() {
    menu?.button.focus({ preventScroll: true });
    setMenu(null);
  }

  async function update(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setProblem("");
    try {
      await action();
      setRevision((value) => value + 1);
      closeMenu();
    } catch {
      setProblem("Could not update the sticker pack.");
    } finally {
      setBusy(false);
    }
  }

  function updatePack(updated: Pack) {
    setPacks((current) => current.map((p) => p.id === updated.id ? updated : p));
  }

  function saveName(target: Pack) {
    if (name.trim()) void update(async () => updatePack((await renamePack(target.id, name.trim())).pack));
  }

  function movePack(target: Pack, delta: number) {
    const ids = packs.map(({ id }) => id);
    const from = ids.indexOf(target.id);
    const to = from + delta;
    if (from < 0 || to < 0 || to >= ids.length) return;
    [ids[from], ids[to]] = [ids[to], ids[from]];
    void update(async () => setPacks((await reorderPacks(ids)).packs));
  }

  function removePack(target: Pack) {
    if (!window.confirm("Remove this sticker pack?")) return;
    void update(async () => {
      await deletePack(target.id);
      setPacks((current) => current.filter(({ id }) => id !== target.id));
      select("recent");
      refresh();
    });
  }

  function removeSticker(item: Selection) {
    if (!item.pack || !window.confirm("Remove this sticker from its pack?")) return;
    const pack = item.pack;
    void update(async () => {
      updatePack((await deleteSticker(pack, item.id)).pack);
      refresh();
    });
  }

  const mediaProps = {
    kind: "sticker" as const, favourites, blocked, onPick, onFavourite,
    onMenu: (item: Selection, button: HTMLElement) => setMenu({ kind: "sticker", item, button }),
  };

  return (
    <>
      <div className="picker-categories" role="group" aria-label="Sticker categories">
        <CategoryButton label="Recent" icon="◷" active={!searching && category === "recent"} onClick={() => select("recent")} />
        <CategoryButton label="Favourites" icon="★" active={!searching && category === "favourites"}
          onClick={() => select("favourites")} />
        {packs.map(({ id, name, icon_url }) => (
          <CategoryButton key={id} label={name} icon="◇" image={icon_url} active={!searching && category === id}
            onClick={() => select(id)} />
        ))}
        <button type="button" className="picker-category" aria-label="Add sticker pack" title="Add sticker pack"
          onClick={(event) => setMenu({ kind: "add", button: event.currentTarget })}>+</button>
      </div>
      <PickerSearch label="Search stickers" value={query} maxLength={ONLINE_QUERY_LIMIT}
        onChange={(value) => { setQuery(value); setProblem(""); }} />
      {!searching && pack && (
        <div className="picker-pack-heading">
          <span translate="no">{pack.name}</span>
          <button type="button" onClick={(event) => {
            setName(pack.name);
            setProblem("");
            setMenu({ kind: "pack", button: event.currentTarget });
          }}>Manage pack</button>
        </div>
      )}
      <div key={`${category}:${trimmed}`} ref={scroller} className="picker-scroll" {...grid}>
        <MediaGrid {...mediaProps} items={ownResults} />
        {ownResults.length > 0 && online.length > 0 && <h3>Online stickers</h3>}
        <MediaGrid {...mediaProps} items={online} first={!ownResults.length} />
        {problem && !menu && <p role="status">{problem}</p>}
        {discovery.result?.status === "unreachable" && <p role="status">{ONLINE_UNAVAILABLE}</p>}
        {waiting ? <p role="status">Loading…</p> : !ownResults.length && !online.length && (
          <p role="status">{searching ? "No stickers found." : category === "recent" ? "No recent stickers." :
            category === "favourites" ? "No favourite stickers." : "This pack is empty."}</p>
        )}
        {discovery.result?.has_more && !waiting && discovery.result.status !== "disabled" && (
          <button type="button" className="picker-more" onClick={discovery.more}>Load more</button>
        )}
      </div>
      <Attribution lines={discovery.result?.attribution ?? []} />
      {menu?.kind === "add" && <PackImport close={closeMenu} onAdded={(added) => {
        setPacks((current) => [...current, added]);
        setRevision((value) => value + 1);
        select(added.id);
        closeMenu();
      }} />}
      {menu?.kind === "pack" && pack && (
        <PickerMenu label="Manage pack" close={closeMenu}>
          <label>
            Pack name
            <input className="picker-search" value={name} disabled={busy}
              onChange={(event) => setName(event.currentTarget.value)}
              onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); saveName(pack); } }} />
          </label>
          <div className="picker-menu-actions">
            <button type="button" onClick={() => saveName(pack)} disabled={busy || !name.trim()}>Rename pack</button>
            <button type="button" onClick={() => movePack(pack, -1)} disabled={busy || packs[0] === pack}>Move earlier</button>
            <button type="button" onClick={() => movePack(pack, 1)} disabled={busy || packs.at(-1) === pack}>Move later</button>
            <button type="button" onClick={() => removePack(pack)} disabled={busy}>Remove pack</button>
          </div>
          {problem && <p role="status">{problem}</p>}
        </PickerMenu>
      )}
      {menu?.kind === "sticker" && (
        <PickerMenu label="Sticker options" close={closeMenu}>
          <div className="picker-menu-actions">
            <button type="button" disabled={busy} onClick={() => { onFavourite(menu.item); closeMenu(); }}>
              {favourites.has(selectionKey(menu.item)) ? "Remove from favourites" : "Add to favourites"}
            </button>
            {menu.item.pack && <button type="button" disabled={busy} onClick={() => removeSticker(menu.item)}>Remove sticker</button>}
          </div>
          {problem && <p role="status">{problem}</p>}
        </PickerMenu>
      )}
    </>
  );
}
