"""Cross-chat file recall (MH 2026-11-13, option C).

"Did you like the cat pic I sent?" in a NEW chat: file recall used to look
only inside the current chat, and cross-chat memory only carries the words -
so the built-in model made up "a white cat with black markings" for a simple
orange drawing. Every file already records its chat, turn, number, date and
description; this module finds the one the message means in the user's OTHER
chats (by meaning, the memory sidecar's embedder) and brings back:
- by default only what she noted about it then (one line of text, cheap), with
  an honest label: from which chat and day, the user did NOT send it here and
  she cannot see it now;
- the real file (a picture, a short document) only when the user asks her to
  really look at it / read it again (meaning.asks_to_look_again).

Only when nothing in THIS chat was recalled, only while cross-chat search is
on (Settings -> Preferences, cross_tab.search_on()), and with a stricter bar
than in-chat recall: a wrong file from another chat confuses more. The
sidecar off / slow: nothing (her cross-chat memory and its label stay).
Every wording and limit lives in TUNABLES.
"""

from datetime import datetime, timedelta

import cross_tab
import meaning
import memory as store

# =============================================================================
# TUNABLES
# =============================================================================

ENABLED = True
DAYS = 30                 # files sent in other chats this recently
MAX_FILES = 60            # newest files looked at, at most (described once, then remembered)
# A file of another chat must fit at least PICK_MIN and beat the next file by
# PICK_MARGIN. Measured 2026-11-13 (858 lines, 18 chats, the files treated as
# another chat's): MIN 0.50 -> 95/174 references, 0.40 -> 122/174; 0 wrong
# files and ~19/684 stray recalls (mostly on-topic) at both. The lead does
# the guarding; the minimum only shut out languages the embedder scores low.
PICK_MIN = 0.40
PICK_MARGIN = 0.12
FILE_DOC_MAX_TOKENS = 3000   # a longer document comes back as its note only
CONTEXT_MESSAGES = 2      # the user's messages before, read along when the message alone is unclear
# Two levels (MH 2026-11-15, option A - food talk at fit 0.41 kept recalling
# "Picture 1 from another chat"):
# 1. the message talks about a file / asks her to look again -> full recall
#    (above: PICK_MIN, its note line or the file, follow-ups);
# 2. no file talk, only the same topic -> a light memory HINT, only when the
#    fit is strong (HINT_MIN), at most once per file per chat, never the
#    file itself, never follow-ups. She may bring it up or ignore it.
# Measured 2026-11-15 (EN/ZH/JA, test sidecar): one food picture in another
# chat - plain food talk 22 lines: hints 4 at 0.55, 0 at 0.60 (it was 15
# full recalls before); specific mentions ("craving katsu curry again"):
# 6/7 at 0.55, 5/7 at 0.60. Script references 39: 25 full + 5 hints at
# 0.60, 0 wrong files.
HINTS = True
HINT_MIN = 0.60
HINT_ONCE_PER_CHAT = True
# "Look again" with no talk about a file they sent (MH 2026-11-19, option A):
# "Wait, let me show you now!" (about to send a NEW lunch photo) read as
# "look again" (0.68 vs 0.55) and the lunch talk fit a food plate of another
# chat at 0.41 (with the messages before) -> the old plate was sent to her as
# the real picture. A look-only message is never read with the messages
# before, and brings the real file only when it fits it at least
# LOOK_ONLY_MIN; between PICK_MIN and that, only her note line. Measured on
# the real sidecar: that message 0.40-0.41; "前に送ったハッシュドポテトの写真、
# もう一回見て" 0.44 (the note); "the wedding dress figurine photo again" 0.68.
LOOK_ONLY_MIN = 0.50

NOTE_LINE = (
    "[RECALLED FROM ANOTHER CHAT: {item} from your chat \"{title}\", sent {day}. The user did "
    "NOT send it here and you cannot see it now. What you noted about it then: {desc}. Do not "
    "describe more of it than this; if they want you to really look at it again, they can send "
    "it here.]")
NO_DESC = "(no description was kept)"
FILE_NOTE = (" (RECALLED FROM ANOTHER CHAT \"{title}\", sent {day} - the user did NOT send it "
             "here; brought back because they asked you to look at it again)")
HINT_LINE = (
    "[MEMORY HINT: this topic may remind you of a {kind} the user showed you in your chat "
    "\"{title}\" on {day}: {desc}. They did NOT send it here and you cannot see it now. If "
    "their message is about it, this is all you noted; otherwise mention it only if it fits "
    "naturally. Describe no more than this.]")

# =============================================================================


def _title(conv_id) -> str:
    try:
        for c in store.list_conversations():
            if int(c.get("id")) == int(conv_id):
                return str(c.get("title") or "untitled")
    except Exception:
        pass
    return "untitled"


def _day(row) -> str:
    return str(row.get("created_at") or "")[:10] or "earlier"


def _candidates(conv, now=None) -> list:
    since = ((now or datetime.now()) - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    rows = store.attachments_where(
        "conversation_id IS NOT NULL AND conversation_id != ? AND message_id IS NOT NULL "
        "AND created_at >= ?", (int(conv), since))
    return rows[-MAX_FILES:]


def _winner(text, rows, descs, least=None):
    """(index of the file the text fits clearly best, its fit) or (None, best)."""
    scores = meaning.fits(text, descs)
    if not scores:
        return None, 0.0
    ranked = sorted(zip(scores, range(len(rows))), reverse=True)
    best, i = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    if best < (PICK_MIN if least is None else least) or best - second < PICK_MARGIN:
        return None, best
    return i, best


_hinted = {}   # chat -> ids of files already hinted at there (per process)


def pick(text, conv, now=None, context=()):
    """(row, "note" | "file" | "hint") for the file of another chat the
    message is about, or (None, None). Full recall ("note" / "file") only
    when the message talks about a file they sent or asks her to look
    again; plain topic talk gets at most a "hint" (HINT_MIN, once per file
    per chat). A message about a file that does not point to one by itself
    ("what did it look like in the photo I sent?") is read together with
    the user's messages just before it (`context`, newest last;
    CONTEXT_MESSAGES of them) - "do you remember my cat's name?" then
    points to the cat (MH 2026-11-13, option A). The bar stays as strict."""
    if not ENABLED or not cross_tab.search_on() or not (text or "").strip():
        return None, None
    if meaning.other_media(text) is True:        # about some other video / photo
        return None, None
    import attachments   # late: attachments imports this module
    rows = _candidates(conv, now)
    if not rows:
        return None, None
    descs = [attachments.describe(r) for r in rows]
    look = meaning.asks_to_look_again(text) is True
    file_talk = bool(meaning.shared_file_kind(text))
    talk = file_talk or look
    if not talk and not HINTS:
        return None, None
    i, best = _winner(text, rows, descs, PICK_MIN if talk else HINT_MIN)
    how = ""
    before = [c for c in list(context or [])[-CONTEXT_MESSAGES:] if (c or "").strip()]
    if i is None and before and file_talk:
        i, best = _winner(" ".join(before + [text]), rows, descs)
        how = ", with the messages before"
    if i is None:
        return None, None
    row = dict(rows[i])
    title, day = _title(row.get("conversation_id")), _day(row)
    row["_other_chat"] = True
    if not talk:
        seen = _hinted.setdefault(conv, set())
        if HINT_ONCE_PER_CHAT and row.get("id") in seen:
            return None, None
        seen.add(row.get("id"))
        while len(_hinted) > 64:
            _hinted.pop(next(iter(_hinted)))
        attachments._log_recall(row, f"memory hint from another chat \"{title}\" (fit {best:.2f})")
        return row, "hint"
    row["_note"] = FILE_NOTE.format(title=title, day=day)
    if not file_talk:
        row["_look_only"] = True       # a vague "look" next turn does not keep it (attachments._follow_up)
    mode = "note"
    if look and row.get("kind") in ("picture", "document") and (file_talk or best >= LOOK_ONLY_MIN):
        if row.get("kind") == "picture" or int(row.get("tokens") or 0) <= FILE_DOC_MAX_TOKENS:
            mode = "file"
    attachments._log_recall(row, f"from another chat \"{title}\" ({mode}, fit {best:.2f}{how})")
    return row, mode


def _desc(row) -> str:
    desc = str(row.get("caption") or "")
    if not desc and row.get("kind") == "document":
        import attachments
        try:
            desc = attachments.document_text(row)
        except Exception:
            desc = ""
    return " ".join(desc.split())[:300] or NO_DESC


def note_line(row) -> str:
    """Her line for a file of another chat she cannot see now."""
    import source_tags
    return NOTE_LINE.format(item=source_tags.item_name(row),
                            title=_title(row.get("conversation_id")), day=_day(row),
                            desc=_desc(row))


def hint_line(row) -> str:
    """Her light memory hint (level 2): no file talk, only the same topic."""
    return HINT_LINE.format(kind=str(row.get("kind") or "file"),
                            title=_title(row.get("conversation_id")), day=_day(row),
                            desc=_desc(row))
