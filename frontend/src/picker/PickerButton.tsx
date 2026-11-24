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
  CATEGORY_ICON_SIZE, CATEGORY_SIZE, EMOJI_CELL, MEDIA_CELL, PANEL_GAP, PANEL_HEIGHT,
  PANEL_WIDTH, PHONE_PANEL_HEIGHT, PHONE_WIDTH, SEARCH_HEIGHT, STICKER_CELL, TAB_HEIGHT, TILE_ACTION_SIZE,
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

export function PickerButton({ disabled, ...actions }: Actions & { disabled: boolean }) {
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
        <span aria-hidden="true">☺</span>
      </button>
      {open && createPortal(<PickerPanel id={id} trigger={trigger} close={close} {...actions} />, document.body)}
    </>
  );
}

function PickerPanel({ id, trigger, close, onEmoji, onMedia, mediaBlocked }: Actions & {
  id: string; trigger: RefObject<HTMLButtonElement | null>; close: (restoreFocus?: boolean) => void;
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
    const root = panel.current;
    if (!root) return;
    const mobile = phoneMedia();
    function place() {
      const anchor = trigger.current?.getBoundingClientRect();
      const viewport = document.documentElement;
      if (!root || !anchor) return;
      const phone = mobile.matches;
      root.classList.toggle("picker-phone", phone);
      if (phone) {
        const keyboard = window.visualViewport;
        const usableHeight = keyboard?.height ?? viewport.clientHeight;
        root.style.bottom = `${Math.max(0, viewport.clientHeight - usableHeight - (keyboard?.offsetTop ?? 0))}px`;
        root.style.left = "0";
        root.style.width = "100%";
        root.style.height = `${Math.round(usableHeight * PHONE_PANEL_HEIGHT / 100)}px`;
      } else {
        const width = Math.min(PANEL_WIDTH, viewport.clientWidth - PANEL_GAP * 2);
        root.style.left = `${Math.max(PANEL_GAP, Math.min(anchor.left, viewport.clientWidth - width - PANEL_GAP))}px`;
        root.style.bottom = `${viewport.clientHeight - anchor.top + PANEL_GAP}px`;
        root.style.width = `${width}px`;
        root.style.height = `${Math.min(PANEL_HEIGHT, Math.max(0, anchor.top - PANEL_GAP * 2))}px`;
      }
    }
    place();
    root.querySelector<HTMLElement>('[aria-selected="true"]')?.focus({ preventScroll: true });
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    const composer = trigger.current?.closest(".composer");
    if (composer) resize?.observe(composer);
    window.addEventListener("resize", place);
    window.visualViewport?.addEventListener("resize", place);
    window.visualViewport?.addEventListener("scroll", place);
    return () => {
      resize?.disconnect();
      window.removeEventListener("resize", place);
      window.visualViewport?.removeEventListener("resize", place);
      window.visualViewport?.removeEventListener("scroll", place);
    };
  }, [trigger]);

  useEffect(() => {
    active.current = true;
    function outside(event: PointerEvent) {
      if (event.target instanceof Node && !panel.current?.contains(event.target) && !trigger.current?.contains(event.target)) {
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
      <div className="picker-tabs" role="tablist" aria-label="Picker tabs" onKeyDown={tabKeys}>
        {PICKER_TABS.map(({ id: key, label }) => (
          <button type="button" role="tab" key={key} id={`${id}-${key}`} aria-selected={tab === key}
            aria-controls={`${id}-content`} tabIndex={tab === key ? 0 : -1} onClick={() => select(key)}>{label}</button>
        ))}
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
