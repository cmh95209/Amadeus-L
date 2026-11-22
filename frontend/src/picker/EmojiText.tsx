// Only emoji presentation sequences get the emoji font. Its keycap subset
// also covers ordinary digits, # and *, so keep normal text in the chat font.
const glyph = "(?:\\p{Emoji}\\uFE0F|\\p{Emoji_Presentation}(?!\\uFE0E))\\p{Emoji_Modifier}?";
const emoji = new RegExp(`(\\p{Regional_Indicator}{2}|[#*0-9]\\uFE0F?\\u20E3|${glyph}(?:\\u200D${glyph})*(?:[\\u{E0020}-\\u{E007E}]+\\u{E007F})?)`, "u");

export function EmojiText({ text }: { text: string }) {
  const parts = text.split(emoji);
  if (parts.length === 1) return text;
  return parts.map((part, i) => i % 2 ? <span className="picker-emoji-font" key={i}>{part}</span> : part);
}
