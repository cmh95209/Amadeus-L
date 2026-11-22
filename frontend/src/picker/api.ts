import { API_BASE } from "../api";
import type { PictureInfo } from "../attachments";

type Provider = "giphy" | "klipy";
export type MediaKind = "gif" | "sticker";
export type Selection = {
  source: "gif" | "online-sticker" | "sticker";
  id: string;
  pack?: string;
  provider?: Provider;
  title?: string;
  url?: string;
  preview?: string;
  local_url?: string;
};
type RecentEmoji = { source: "emoji"; id: string };
export type PickerState = {
  enabled: boolean;
  recent: Record<MediaKind, Selection[]> & { emoji: RecentEmoji[] };
  favourites: Record<MediaKind, Selection[]>;
};
export type DiscoveryItem = {
  id: string; provider: Provider; title: string; preview: string; full: string;
  width: number; height: number;
};
export type ProviderCategory = { provider: Provider; name: string; query: string; preview: string | null };
export type Discovery<T> = {
  status: "ok" | "disabled" | "unreachable";
  items: T[];
  attribution: string[];
  page: number;
  has_more: boolean;
};
export type Sticker = { file: string; name: string; favourite: boolean; full: string; thumb: string };
export type Pack = { id: string; name: string; icon_url: string | null; stickers: Sticker[] };

class PickerError extends Error {
  constructor(readonly code: string) { super(code); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}/picker${path}`, init);
  const data = await response.json();
  if (!response.ok || data.status === "error" || data.ok === false) throw new PickerError(data.error);
  return data as T;
}

function json(method: "POST" | "PATCH", data: unknown): RequestInit {
  return { method, headers: { "content-type": "application/json" }, body: JSON.stringify(data) };
}

const PACK_TOO_LARGE = "The sticker pack or one of its files is too large.";
const PACK_NO_IMAGES = "Choose at least one sticker image.";

export function packProblem(error: unknown) {
  if (error instanceof PickerError) {
    switch (error.code) {
      case "picker_sticker_too_large":
      case "picker_pack_limit":
      case "picker_archive_limit":
        return PACK_TOO_LARGE;
      case "picker_files_required":
        return PACK_NO_IMAGES;
    }
  }
  return "Could not import the sticker pack. Check the files and try again.";
}

function discoveryPath(operation: string, args: Record<string, string>) {
  return `/online/${operation}?${new URLSearchParams(args)}`;
}

export const getPickerState = (signal: AbortSignal) => request<PickerState>("/state", { signal });
export const rememberEmoji = (id: string) => request<PickerState>("/recent", json("POST", { source: "emoji", id }));
export const setFavourite = (item: Selection, favourite: boolean) =>
  request<PickerState>("/favourite", json("POST", { ...item, favourite }));
export const getOnlineSetting = () => request<{ enabled: boolean }>("/setting");
export const saveOnlineSetting = (enabled: boolean) => request<{ enabled: boolean }>("/setting", json("POST", { enabled }));
export const getCategories = (kind: MediaKind, lang: string, signal: AbortSignal) =>
  request<Discovery<ProviderCategory>>(discoveryPath("categories", { kind, lang }), { signal });
export const discover = (kind: MediaKind, lang: string, query: string, page: number, signal: AbortSignal) =>
  request<Discovery<DiscoveryItem>>(discoveryPath(query ? "search" : "trending",
    { kind, lang, q: query, page: String(page) }), { signal });
export const prepareSelection = (item: Selection, conversation_id: number | null) =>
  request<PictureInfo>("/send", json("POST", { ...item, conversation_id }));

export const getPacks = (query: string, signal: AbortSignal) =>
  request<{ packs: Pack[] }>(`/packs?${new URLSearchParams({ q: query })}`, { signal });
export const importPack = (body: FormData) => request<{ pack: Pack }>("/packs/import", { method: "POST", body });
const packPath = (id: string) => `/packs/${encodeURIComponent(id)}`;
export const renamePack = (id: string, name: string) => request<{ pack: Pack }>(packPath(id), json("PATCH", { name }));
export const deletePack = (id: string) => request(packPath(id), { method: "DELETE" });
export const deleteSticker = (id: string, file: string) =>
  request<{ pack: Pack }>(`${packPath(id)}/stickers/${encodeURIComponent(file)}`, { method: "DELETE" });
export const reorderPacks = (ids: string[]) => request<{ packs: Pack[] }>("/packs/reorder", json("POST", { ids }));

export function selectionKey({ source, provider, pack, id }: Selection) {
  return JSON.stringify([source, provider, pack, id]);
}

export function onlineItem(item: DiscoveryItem, kind: MediaKind): Selection {
  return {
    source: kind === "gif" ? "gif" : "online-sticker",
    id: item.id, provider: item.provider, title: item.title, url: item.full, preview: item.preview,
  };
}

export function ownSticker(pack: Pack, sticker: Sticker): Selection {
  return { source: "sticker", pack: pack.id, id: sticker.file, title: sticker.name,
    url: sticker.full, preview: sticker.thumb };
}
