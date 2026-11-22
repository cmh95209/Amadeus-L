import { useEffect, useRef, useState } from "react";
import { absoluteUrl } from "../attachments";
import { selectionKey } from "./api";
import type { MediaKind, Selection } from "./api";
import { useLongPress } from "./interaction";
import { PREVIEW_MARGIN } from "./tunables";

type Observe = (element: HTMLElement, changed: (visible: boolean) => void) => () => void;

function usePreviews() {
  const observer = useRef<IntersectionObserver | null>(null);
  const targets = useRef(new Map<Element, (visible: boolean) => void>());
  const observe = useRef<Observe>((element, changed) => {
    if (typeof IntersectionObserver === "undefined") {
      changed(true);
      return () => undefined;
    }
    observer.current ??= new IntersectionObserver((entries) => {
      entries.forEach((entry) => targets.current.get(entry.target)?.(entry.isIntersecting));
    }, { root: element.closest(".picker-scroll"), rootMargin: `${PREVIEW_MARGIN}px` });
    targets.current.set(element, changed);
    observer.current.observe(element);
    return () => {
      observer.current?.unobserve(element);
      targets.current.delete(element);
      if (targets.current.size === 0) {
        observer.current?.disconnect();
        observer.current = null;
      }
    };
  });
  return observe.current;
}

function Preview({ item, observe }: { item: Selection; observe: Observe }) {
  const root = useRef<HTMLSpanElement>(null);
  const [visible, setVisible] = useState(false);
  const [failed, setFailed] = useState<string[]>([]);
  useEffect(() => root.current ? observe(root.current, setVisible) : undefined, [observe]);
  const first = absoluteUrl(item.local_url || item.preview || item.url);
  const fallback = item.local_url ? absoluteUrl(item.preview || item.url) : undefined;
  const url = [first, fallback].find((candidate) => candidate && !failed.includes(candidate));
  return (
    <span ref={root} className="picker-preview" aria-hidden="true">
      {visible && url
        ? <img src={url} alt="" loading="lazy" decoding="async" onError={() => setFailed((urls) => [...urls, url])} />
        : visible && <span>{item.source === "gif" ? "GIF" : "◇"}</span>}
    </span>
  );
}

export function MediaGrid({ kind, items, favourites, blocked, first = true, onPick, onFavourite, onMenu }: {
  kind: MediaKind; items: Selection[]; favourites: Set<string>; blocked: boolean; first?: boolean;
  onPick: (item: Selection) => void; onFavourite: (item: Selection) => void;
  onMenu?: (item: Selection, button: HTMLElement) => void;
}) {
  const observe = usePreviews();
  const press = useLongPress();
  if (!items.length) return null;
  return (
    <div className={`picker-grid picker-${kind}-grid`}>
      {items.map((item, i) => {
        const key = selectionKey(item);
        const starred = favourites.has(key);
        const starLabel = starred ? "Remove from favourites" : "Add to favourites";
        const name = item.title || (kind === "gif" ? "GIF" : "Sticker");
        return (
          <div key={key} className="picker-media-tile">
            <button type="button" data-picker-cell className="picker-media-cell"
              tabIndex={first && i === 0 ? 0 : -1} title={name} aria-label={name} aria-disabled={blocked}
              aria-keyshortcuts={onMenu ? "shift+f10" : undefined}
              {...(onMenu ? press.bind((button) => onMenu(item, button)) : {})}
              onClick={(event) => {
                if (!press.consume(event.currentTarget, event.detail) && !blocked) onPick(item);
              }}>
              <Preview item={item} observe={observe} />
            </button>
            <button type="button" data-picker-action tabIndex={first && i === 0 ? 0 : -1}
              className="picker-tile-action" title={onMenu ? "Sticker options" : starLabel}
              aria-label={onMenu ? "Sticker options" : starLabel} aria-pressed={onMenu ? undefined : starred}
              onClick={(event) => onMenu ? onMenu(item, event.currentTarget) : onFavourite(item)}>
              {onMenu ? (starred ? "★" : "⋯") : (starred ? "★" : "☆")}
            </button>
          </div>
        );
      })}
    </div>
  );
}

const PROVIDER_CREDITS = ["Powered by KLIPY", "Powered by GIPHY"];

export function Attribution({ lines }: { lines: string[] }) {
  const credits = PROVIDER_CREDITS.filter((credit) => lines.includes(credit));
  return credits.length > 0 && (
    <div className="picker-attribution">
      {credits.map((credit) => <span key={credit} translate="no">{credit}</span>)}
    </div>
  );
}
