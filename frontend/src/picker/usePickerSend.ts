import { useEffect, useRef, useState } from "react";
import { canSend, removeAttachment } from "../attachments";
import type { PictureInfo, TrayCard } from "../attachments";
import { prepareSelection } from "./api";
import type { Selection } from "./api";
import { SEND_ON_PICK } from "./tunables";

export type PickerMessage = { text: string; speechReady?: Promise<boolean> };
type Ticket = PickerMessage & { conversation: number | null; key: string };

export function usePickerSend({ cards, conversation, getConversation, getText, unlockAudio, stage, canReply, send }: {
  cards: TrayCard[];
  conversation: number | null;
  getConversation: () => number | null;
  getText: () => string;
  unlockAudio: () => Promise<boolean> | undefined;
  stage: (picture: PictureInfo, react: boolean) => string;
  canReply: boolean;
  send: (message: PickerMessage) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [ticket, setTicket] = useState<Ticket>();
  const locked = useRef(false);

  function release() {
    locked.current = false;
    setBusy(false);
    setTicket(undefined);
  }

  async function pick(item: Selection) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    const conversation = getConversation();
    const text = getText().trim();
    const speechReady = SEND_ON_PICK ? unlockAudio() : undefined;
    try {
      const picture = await prepareSelection(item, conversation);
      if (getConversation() !== conversation) {
        void removeAttachment(picture.id).catch(() => undefined);
        throw new Error();
      }
      const key = stage(picture, !SEND_ON_PICK);
      if (SEND_ON_PICK) setTicket({ key, conversation, text, speechReady });
      else release();
    } catch (error) {
      release();
      throw error;
    }
  }

  useEffect(() => {
    if (!ticket) return;
    const card = cards.find(({ key }) => key === ticket.key);
    if (!card || card.state === "error" || conversation !== ticket.conversation) {
      release();
    } else if (card.state === "ready" && canReply && canSend(ticket.text, cards)) {
      release();
      send(ticket);
    }
  }, [ticket, cards, conversation, canReply, send]);

  return { pick, busy, locked };
}
