import type { CSSProperties } from "react";
import { absoluteUrl } from "../attachments";
import type { PictureInfo } from "../attachments";
import { CHAT_GIF_HEIGHT, CHAT_GIF_WIDTH, CHAT_STICKER_SIZE } from "./tunables";

const sizes = {
  "--chat-gif-width": `${CHAT_GIF_WIDTH}px`,
  "--chat-gif-height": `${CHAT_GIF_HEIGHT}px`,
  "--chat-sticker-size": `${CHAT_STICKER_SIZE}px`,
} as CSSProperties;

export function ChatMedia({ picture, onOpen }: { picture: PictureInfo; onOpen: (picture: PictureInfo) => void }) {
  const sticker = picture.media_type === "sticker";
  const video = !sticker && picture.gif ? absoluteUrl(picture.video_url) : undefined;
  const image = absoluteUrl(picture.original_url || picture.url || picture.thumb_url);
  return (
    <button type="button" className={`picture-thumb ${sticker ? "picker-chat-sticker" : "picker-chat-gif"}`}
      style={sizes} aria-label={sticker ? `Open sticker ${picture.name}` : `Open GIF ${picture.name}`}
      title={picture.caption || picture.name} onClick={() => onOpen(picture)}>
      {video
        ? <video src={video} poster={absoluteUrl(picture.thumb_url)} muted autoPlay loop playsInline aria-hidden="true" />
        : <img src={image} alt="" loading="lazy" decoding="async" />}
      {!sticker && picture.number != null && <span className="picture-number">{picture.number}</span>}
    </button>
  );
}
