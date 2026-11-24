import { useCallback, useEffect, useRef, useState } from "react";
import { discover, getCategories, onlineItem, selectionKey } from "./api";
import type { Discovery, MediaKind, ProviderCategory, Selection } from "./api";
import { FIRST_PAGE } from "./tunables";

type Results = Discovery<Selection>;
type SearchRun = { key: string; result?: Results; loading: boolean };

export function useProviderCategories(enabled: boolean, lang: string) {
  const [state, setState] = useState<{ lang: string; result: Discovery<ProviderCategory> }>();
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void getCategories("gif", lang, controller.signal)
      .then((result) => { if (!controller.signal.aborted) setState({ lang, result }); })
      .catch(() => undefined);
    return () => controller.abort();
  }, [enabled, lang]);
  return enabled && state?.lang === lang ? state.result : undefined;
}

const unavailable: Results = {
  items: [], status: "unreachable", page: FIRST_PAGE, has_more: false, attribution: [],
};

export function useDiscovery(kind: MediaKind, lang: string, query: string, enabled: boolean) {
  const key = JSON.stringify([kind, lang, query]);
  const [state, setState] = useState<SearchRun>();
  const request = useRef<AbortController | null>(null);
  const run = useCallback((page: number, previous?: Results) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setState({ key, result: previous, loading: true });
    void discover(kind, lang, query, page, controller.signal)
      .then((raw) => {
        if (controller.signal.aborted) return;
        const items = raw.items.map((item) => onlineItem(item, kind));
        if (previous && raw.status !== "ok") {
          setState({ key, result: { ...previous, status: raw.status }, loading: false });
          return;
        }
        const byId = new Map((previous?.items ?? []).map((item) => [selectionKey(item), item]));
        items.forEach((item) => byId.set(selectionKey(item), item));
        const attribution = [...new Set([...(previous?.attribution ?? []), ...raw.attribution])];
        setState({ key, result: { ...raw, items: [...byId.values()], attribution }, loading: false });
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setState({ key, result: { ...(previous ?? unavailable), status: "unreachable" }, loading: false });
      });
  }, [key, kind, lang, query]);

  useEffect(() => {
    if (!enabled) return;
    run(FIRST_PAGE);
    return () => request.current?.abort();
  }, [enabled, run]);

  const current = enabled && state?.key === key ? state : undefined;
  return {
    result: current?.result,
    loading: enabled && (current?.loading ?? true),
    more: () => {
      const result = current?.result;
      if (result?.has_more && !current?.loading) run(result.page + 1, result);
    },
  };
}
