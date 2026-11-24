import { useEffect, useSyncExternalStore } from "react";
import { getOnlineSetting, saveOnlineSetting } from "./api";

export type EmojiStyle = "noto" | "system";
const STYLE_KEY = "amadeus.emojiStyle";
const TAB_KEY = "amadeus.pickerTab";
const TONES_KEY = "amadeus.emojiTones";
export const PICKER_TABS = [
  { id: "emoji", label: "Emoji" }, { id: "gif", label: "GIF" }, { id: "sticker", label: "Sticker" },
] as const;
export type PickerTab = typeof PICKER_TABS[number]["id"];
let selectedStyle: EmojiStyle | undefined;
let selectedTab: PickerTab | undefined;
let selectedTones: Record<string, string> | undefined;

function read(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function write(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* Storage is optional. */ }
}

export function emojiStyle(): EmojiStyle {
  return selectedStyle ?? (read(STYLE_KEY) === "system" ? "system" : "noto");
}

export function applyEmojiStyle(style = emojiStyle()) {
  selectedStyle = style;
  document.documentElement.dataset.emojiStyle = style;
}

export function saveEmojiStyle(style: EmojiStyle) {
  write(STYLE_KEY, style);
  applyEmojiStyle(style);
}

export function readTab(): PickerTab {
  return selectedTab ?? PICKER_TABS.find(({ id }) => id === read(TAB_KEY))?.id ?? "emoji";
}

export function saveTab(tab: PickerTab) {
  selectedTab = tab;
  write(TAB_KEY, tab);
}

export function readTones(): Record<string, string> {
  if (selectedTones) return selectedTones;
  try {
    const value: unknown = JSON.parse(read(TONES_KEY) ?? "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, string> : {};
  } catch { return {}; }
}

export function saveTones(tones: Record<string, string>) {
  selectedTones = tones;
  write(TONES_KEY, JSON.stringify(tones));
}

let onlineEnabled = true;
let revision = 0;
let settingRead: Promise<void> | undefined;
const listeners = new Set<() => void>();

function publish(value: boolean) {
  onlineEnabled = value;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export async function changeOnlineSetting(value: boolean) {
  const current = ++revision;
  try {
    const { enabled } = await saveOnlineSetting(value);
    if (revision === current) publish(enabled);
  } finally {
    if (revision === current) revision++;
  }
}

export function useOnlineSetting() {
  useEffect(() => {
    const current = revision;
    settingRead ??= getOnlineSetting()
      .then(({ enabled }) => { if (revision === current) publish(enabled); })
      .catch(() => undefined)
      .finally(() => { settingRead = undefined; });
  }, []);
  return useSyncExternalStore(subscribe, () => onlineEnabled, () => true);
}
