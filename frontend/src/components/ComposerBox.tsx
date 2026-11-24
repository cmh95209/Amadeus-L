import {
  forwardRef,
  memo,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { ClipboardEvent } from "react";
import { CHATGPT_USAGE_URL } from "../api";
import ChatGPTComposerPicker from "./ChatGPTComposerPicker";
import type { useChatGPTConnection } from "./ChatGPTConnection";

// The message being typed, kept across a reload: Android may close the app in
// the background (a call, the gallery) and it reloads when you come back.
export const DRAFT_KEY = "amadeus.draft";
const DRAFT_KEEP_MS = 12 * 60 * 60 * 1000;

export function readDraft(): string {
  try {
    const saved = JSON.parse(localStorage.getItem(DRAFT_KEY) ?? "null") as { text?: unknown; at?: number } | null;
    if (!saved || typeof saved.text !== "string" || !saved.at) return "";
    return Date.now() - saved.at < DRAFT_KEEP_MS ? saved.text : "";
  } catch {
    return "";
  }
}

export type ComposerBoxHandle = {
  /** What is typed right now. */
  text: () => string;
  /** Empty the box (after sending). */
  clear: () => void;
  /** Insert at the last cursor position or replace the selection. */
  insert: (text: string) => void;
};

type Props = {
  chatgptMode: boolean;
  chatGPTConnection: ReturnType<typeof useChatGPTConnection>;
  chatGPTModelDisabled: boolean;
  disabled: boolean;
  placeholder: string;
  /** Called only when the box goes from empty to not empty or back. */
  onHasText: (hasText: boolean) => void;
  onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void;
  /** The chat list above: kept scrolled to her newest line as the box grows. */
  getList: () => HTMLElement | null;
};

/**
 * The message box. What is typed lives HERE, not in App: a keystroke used to
 * redraw the whole app with every message of the chat (2026-11-09: 560
 * messages -> 56 ms per key, freezes up to 0.4 s). Now a keystroke redraws
 * this box only; App hears about it only when the box turns empty or not.
 */
const ComposerBox = memo(forwardRef<ComposerBoxHandle, Props>(function ComposerBox(
  { chatgptMode, chatGPTConnection, chatGPTModelDisabled, disabled, placeholder, onHasText, onPaste, getList }, ref,
) {
  const [input, setInput] = useState(readDraft);
  const boxRef = useRef<HTMLTextAreaElement>(null);
  const inputRef = useRef(input);
  inputRef.current = input;
  const onHasTextRef = useRef(onHasText);
  onHasTextRef.current = onHasText;
  const getListRef = useRef(getList);
  getListRef.current = getList;
  const hadText = useRef<boolean | null>(null);

  useImperativeHandle(ref, () => ({
    text: () => inputRef.current,
    clear: () => setInput(""),
    insert: (text) => {
      const box = boxRef.current;
      if (!box || box.disabled) return;
      box.setRangeText(text, box.selectionStart, box.selectionEnd, "end");
      inputRef.current = box.value;
      setInput(box.value);
    },
  }), []);

  useEffect(() => {
    const has = input.trim().length > 0;
    if (hadText.current !== has) {
      hadText.current = has;
      onHasTextRef.current(has);
    }
    try {
      if (input) localStorage.setItem(DRAFT_KEY, JSON.stringify({ text: input, at: Date.now() }));
      else localStorage.removeItem(DRAFT_KEY);
    } catch {
      // Not kept across a reload - harmless.
    }
  }, [input]);

  useLayoutEffect(() => {
    // The box grows with what is typed (the CSS max-height caps it, then it
    // scrolls) and shrinks back once the message is sent. A new screen width
    // re-wraps the lines, so measure again on resize too. The scrollbar only
    // appears once it is capped, so short text uses the box's full width. A
    // growing box shrinks the chat above it: if her newest line was in view,
    // keep it there.
    const box = boxRef.current;
    if (!box) return;
    function fit() {
      const list = getListRef.current();
      const atBottom = !!list && list.scrollHeight - list.clientHeight - list.scrollTop < 40;
      const before = box!.style.height;
      box!.style.height = "auto";
      box!.style.overflowY = "hidden";
      box!.style.height = `${box!.scrollHeight}px`;
      if (box!.scrollHeight > box!.clientHeight + 1) box!.style.overflowY = "auto";
      if (list && atBottom && box!.style.height !== before) list.scrollTop = list.scrollHeight;
    }
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [input]);

  return (
    <>
    <textarea
      ref={boxRef}
      value={input}
      onChange={(event) => setInput(event.target.value)}
      onPaste={onPaste}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          event.currentTarget.form?.requestSubmit();
        }
      }}
      disabled={disabled}
      placeholder={placeholder}
      rows={1}
      aria-disabled={disabled}
    />
    {chatgptMode && (
      <div className="composer-plan">
        <span>Using ChatGPT plan ·</span>
        <a href={CHATGPT_USAGE_URL} target="_blank" rel="noreferrer">Check your ChatGPT usage</a>
        <ChatGPTComposerPicker connection={chatGPTConnection} disabled={chatGPTModelDisabled} />
      </div>
    )}
    </>
  );
}));

export default ComposerBox;
