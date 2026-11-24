import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import type { CSSProperties, KeyboardEvent, RefObject } from "react";
import { currentLanguage, subscribe } from "../i18n";
import type { Selection } from "./api";
import { EmojiTab } from "./EmojiTab";
import { GifTab } from "./GifTab";
import { PICKER_TABS, readTab, saveTab, useOnlineSetting } from "./preferences";
import type { PickerTab } from "./preferences";
import { StickerTab } from "./StickerTab";
import {
  CATEGORY_ICON_SIZE, CATEGORY_SIZE, EMOJI_CELL, MEDIA_CELL,
  PHONE_WIDTH, SEARCH_HEIGHT, STICKER_CELL, TAB_HEIGHT, TILE_ACTION_SIZE,
} from "./tunables";
import { usePickerState } from "./usePickerState";

const phoneMedia = () => window.matchMedia(`(max-width: ${PHONE_WIDTH}px)`);
const dimensions = {
  "--picker-emoji-cell": `${EMOJI_CELL}px`,
  "--picker-media-cell": `${MEDIA_CELL}px`,
  "--picker-sticker-cell": `${STICKER_CELL}px`,
  "--picker-category-size": `${CATEGORY_SIZE}px`,
  "--picker-category-icon-size": `${CATEGORY_ICON_SIZE}px`,
  "--picker-search-height": `${SEARCH_HEIGHT}px`,
  "--picker-tab-height": `${TAB_HEIGHT}px`,
  "--picker-tile-action-size": `${TILE_ACTION_SIZE}px`,
} as CSSProperties;

type Actions = {
  mediaBlocked: boolean;
  onEmoji: (emoji: string) => void;
  onMedia: (item: Selection) => Promise<void>;
};

export function PickerButton({ disabled, dock, ...actions }: Actions & {
  disabled: boolean;
  dock: HTMLDivElement | null;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);

  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  }

  return (
    <>
      <button ref={trigger} type="button" className="attach-button picker-trigger" aria-label="Open emoji, GIF and sticker picker"
        title="Open emoji, GIF and sticker picker" aria-expanded={open} aria-haspopup="dialog" aria-controls={open ? id : undefined}
        disabled={disabled} onClick={() => {
          if (open) { close(); return; }
          if (phoneMedia().matches && document.activeElement instanceof HTMLElement &&
              document.activeElement !== trigger.current) document.activeElement.blur();
          setOpen(true);
        }}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"
          strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="8.5" />
          <path d="M8.5 13.5a4 4 0 0 0 7 0" />
          <path d="M9 9.5h.01m5.99 0h.01" strokeWidth="2.5" />
        </svg>
      </button>
      {open && dock && createPortal(<PickerPanel id={id} dock={dock} trigger={trigger} close={close} {...actions} />, dock)}
    </>
  );
}

function PickerPanel({ id, dock, trigger, close, onEmoji, onMedia, mediaBlocked }: Actions & {
  id: string; dock: HTMLDivElement; trigger: RefObject<HTMLButtonElement | null>; close: (restoreFocus?: boolean) => void;
}) {
  const [tab, setTab] = useState<PickerTab>(readTab);
  const [problem, setProblem] = useState("");
  const [preparing, setPreparing] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  const active = useRef(false);
  const closeRef = useRef(close);
  closeRef.current = close;
  const lang = useSyncExternalStore(subscribe, currentLanguage, () => "en");
  const enabled = useOnlineSetting();
  const picker = usePickerState();

  useLayoutEffect(() => {
    panel.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    active.current = true;
    const composer = trigger.current?.closest(".composer");
    function outside(event: PointerEvent) {
      if (event.target instanceof Node && !panel.current?.contains(event.target) && !composer?.contains(event.target)) {
        closeRef.current();
      }
    }
    function escape(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      closeRef.current(true);
    }
    window.addEventListener("pointerdown", outside);
    window.addEventListener("keydown", escape);
    return () => {
      active.current = false;
      window.removeEventListener("pointerdown", outside);
      window.removeEventListener("keydown", escape);
    };
  }, [trigger]);

  function select(next: PickerTab) {
    setTab(next);
    saveTab(next);
    setProblem("");
  }

  function tabKeys(event: KeyboardEvent<HTMLDivElement>) {
    const index = PICKER_TABS.findIndex(({ id }) => id === tab);
    const last = PICKER_TABS.length - 1;
    const target = { ArrowLeft: index ? index - 1 : last, ArrowRight: index < last ? index + 1 : 0,
      Home: 0, End: last }[event.key];
    if (target === undefined) return;
    event.preventDefault();
    select(PICKER_TABS[target].id);
    event.currentTarget.querySelectorAll("button")[target]?.focus();
  }

  async function pickMedia(item: Selection) {
    if (preparing || mediaBlocked) return;
    setPreparing(true);
    setProblem("");
    try {
      await onMedia(item);
      if (active.current) closeRef.current(true);
    } catch {
      setProblem("Could not attach this item.");
    } finally {
      setPreparing(false);
    }
  }

  const media = { state: picker.state, enabled, lang, blocked: preparing || mediaBlocked,
    onPick: (item: Selection) => void pickMedia(item), onFavourite: (item: Selection) => void picker.favourite(item) };
  return (
    <div ref={panel} id={id} className="picker-panel" style={dimensions} role="dialog" aria-label="Emoji, GIF and sticker picker">
      <div className="picker-header">
        <PickerResizeHandle dock={dock} />
        <div className="picker-tabs" role="tablist" aria-label="Picker tabs" onKeyDown={tabKeys}>
          {PICKER_TABS.map(({ id: key, label }) => (
            <button type="button" role="tab" key={key} id={`${id}-${key}`} aria-selected={tab === key}
              aria-controls={`${id}-content`} tabIndex={tab === key ? 0 : -1} onClick={() => select(key)}>{label}</button>
          ))}
        </div>
        <button className="picker-close" type="button" aria-label="Close" title="Close" onClick={() => close(true)}>
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <div className="picker-content" role="tabpanel" id={`${id}-content`} aria-labelledby={`${id}-${tab}`}>
        {tab === "emoji" && <EmojiTab lang={lang} recent={picker.recentEmoji} onPick={(emoji) => {
          if (phoneMedia().matches && document.activeElement instanceof HTMLInputElement) document.activeElement.blur();
          onEmoji(emoji);
          picker.recent(emoji);
        }} />}
        {tab === "gif" && <GifTab {...media} />}
        {tab === "sticker" && <StickerTab {...media} refresh={picker.refresh} />}
      </div>
      {(problem || picker.problem || preparing) && (
        <p className="picker-feedback" role="status">{problem || picker.problem || "Attaching…"}</p>
      )}
    </div>
  );
}

const minPickerPercent = 20;
const maxPickerPercent = 80;
const clampPickerPercent = (percent: number) =>
  Math.round(Math.min(maxPickerPercent, Math.max(minPickerPercent, percent)));

function PickerResizeHandle({ dock }: { dock: HTMLDivElement }) {
  const [size, setSize] = useState(44);
  const handle = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const bar = handle.current;
    if (!bar) return;
    const room = dock.parentElement?.clientHeight;
    if (room) setSize(clampPickerPercent(100 * dock.clientHeight / room));
    let drag: { pointerId: number; y: number; height: number; room: number } | null = null;
    function start(event: PointerEvent) {
      const available = dock.parentElement?.clientHeight;
      if (drag || event.button !== 0 || !available) return;
      event.preventDefault();
      drag = { pointerId: event.pointerId, y: event.clientY, height: dock.clientHeight, room: available };
      bar!.setPointerCapture(event.pointerId);
    }
    function move(event: PointerEvent) {
      if (drag?.pointerId !== event.pointerId) return;
      const next = clampPickerPercent(100 * (drag.height + drag.y - event.clientY) / drag.room);
      dock.style.setProperty("--picker-height", `${next}cqh`);
      setSize(next);
    }
    function stop() { drag = null; }
    bar.addEventListener("pointerdown", start);
    bar.addEventListener("pointermove", move);
    bar.addEventListener("lostpointercapture", stop);
    return () => {
      bar.removeEventListener("pointerdown", start);
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("lostpointercapture", stop);
      dock.style.removeProperty("--picker-height");
    };
  }, [dock]);

  function key(event: KeyboardEvent<HTMLDivElement>) {
    const room = dock.parentElement?.clientHeight;
    const current = room ? 100 * dock.clientHeight / room : size;
    const next = { ArrowUp: current + 5, ArrowDown: current - 5, PageUp: current + 15,
      PageDown: current - 15, Home: minPickerPercent, End: maxPickerPercent }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    const percent = clampPickerPercent(next);
    dock.style.setProperty("--picker-height", `${percent}cqh`);
    setSize(percent);
  }

  return (
    <div ref={handle} className="picker-resize" role="slider" tabIndex={0}
      aria-label="Resize picker" aria-orientation="vertical"
      aria-valuemin={minPickerPercent} aria-valuemax={maxPickerPercent} aria-valuenow={size} onKeyDown={key}>
      <span aria-hidden="true" />
    </div>
  );
}
