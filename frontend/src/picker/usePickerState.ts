import { useEffect, useRef, useState } from "react";
import { getPickerState, rememberEmoji, selectionKey, setFavourite } from "./api";
import type { PickerState, Selection } from "./api";
import { mergeRecents } from "./emojiData";

const empty: PickerState = {
  enabled: true, recent: { emoji: [], gif: [], sticker: [] }, favourites: { gif: [], sticker: [] },
};

// Serialize mutations so an older response cannot replace a newer recent or
// favourite. Keep the last snapshot across closes and connection failures.
let saved = empty;
let pending: Promise<unknown> = Promise.resolve();
let optimisticEmoji: { id: string; request: symbol }[] = [];

function enqueue<T>(operation: () => Promise<T>): Promise<T> {
  const result = pending.then(operation);
  pending = result.catch(() => undefined);
  return result;
}

export function usePickerState() {
  const [state, setState] = useState(saved);
  const [optimistic, setOptimistic] = useState(optimisticEmoji);
  const [stars, setStars] = useState(new Map<string, { item: Selection; keep: boolean }>());
  const starring = useRef(new Set<string>());
  const [problem, setProblem] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void enqueue(async () => {
      if (controller.signal.aborted) return;
      const fresh = await getPickerState(controller.signal);
      saved = fresh;
      setState(fresh);
      setOptimistic(optimisticEmoji);
    }).catch(() => undefined);
    return () => controller.abort();
  }, [revision]);

  function recent(id: string) {
    const request = Symbol();
    const earlier = new Map(optimisticEmoji.map((entry) => [entry.id, entry]));
    earlier.set(id, { id, request });
    optimisticEmoji = mergeRecents([id], optimisticEmoji.map((entry) => entry.id)).map((key) => earlier.get(key)!);
    setOptimistic(optimisticEmoji);
    void enqueue(async () => {
      const fresh = await rememberEmoji(id);
      optimisticEmoji = optimisticEmoji.filter((entry) => entry.request !== request);
      saved = fresh;
      setState(fresh);
      setOptimistic(optimisticEmoji);
    }).catch(() => undefined);
  }

  async function favourite(item: Selection) {
    const key = selectionKey(item);
    if (starring.current.has(key)) return;
    starring.current.add(key);
    const kind = item.source === "gif" ? "gif" : "sticker";
    const keep = !state.favourites[kind].some((entry) => selectionKey(entry) === key);
    setStars((current) => new Map(current).set(key, { item, keep }));
    setProblem("");
    try {
      await enqueue(async () => {
        saved = await setFavourite(item, keep);
        setState(saved);
      });
    } catch {
      setProblem("Could not update favourites.");
    } finally {
      starring.current.delete(key);
      setStars((current) => {
        const next = new Map(current);
        next.delete(key);
        return next;
      });
    }
  }

  function favourites(kind: "gif" | "sticker") {
    const added = [...stars.values()].flatMap(({ item, keep }) =>
      keep && (item.source === "gif") === (kind === "gif") ? [item] : []);
    return [...added, ...state.favourites[kind].filter((item) => !stars.has(selectionKey(item)))];
  }

  return {
    state: stars.size ? { ...state, favourites: { gif: favourites("gif"), sticker: favourites("sticker") } } : state,
    recent, favourite, problem,
    recentEmoji: mergeRecents(optimistic.map(({ id }) => id), state.recent.emoji.map(({ id }) => id)),
    refresh: () => setRevision((value) => value + 1),
  };
}
