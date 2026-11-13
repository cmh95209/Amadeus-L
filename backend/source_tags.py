"""Source tags: where each thing in her prompt came from (MH 2026-11-13).

A file the user attached just now, one kept on her desk, one her file recall
brought back by matching the user's words (the user did NOT send it again),
a web search she chose to run, one the app ran for her, live weather data,
pages from links the user sent. The tag says which, so she never takes the
app's guess for something the user did - 2026-11-13: "the video was really
relaxing" (a YouTube video) brought back an old mall video of hers, and its
rule told her "you watched the video above".

Every wording lives in the TUNABLES block below; the other modules only call
the helpers. The tags are plain English like the rest of her prompt rules,
with no per-language logic.

Prompt reuse: a desk file next to the message it came with keeps its label
byte-for-byte (no tag); the NEW / RECALLED line goes into the part of the
prompt that changes every turn anyway (attachments._add_turn_text).
"""

# =============================================================================
# TUNABLES
# =============================================================================

# Label notes (go inside the file's own label, after "from the user").
DESK_NOTE = (" (on your desk - the user sent it earlier, at that turn; kept for "
             "reference, not sent again)")
RECALLED_NOTE = (" (RECALLED - your file recall brought this back because the user's "
                 "words seemed to match it; the user did NOT send it again)")

# The line for THIS turn naming what is new and what was recalled.
NEW_LINE = "NEW - the user attached {items} to their newest message."
RECALLED_LINE = (
    "RECALLED - your file recall brought back {items} because the user's words seemed "
    "to match; the user did NOT send {them} again. It may be a wrong match: if their "
    "message is not about {them}, ignore {them} completely and do not mention {them}.")
TURN_HEAD = "[Sources this turn: "
TURN_TAIL = "]"

# Web and tool results (first line of the result message she reads).
WEB_OWN = "[Source: your own web search - you chose to run it.]"
WEB_AUTO = ("[Source: a web search the app ran for you automatically, because the "
            "user's message asked for something to be looked up. The app picked the "
            "search words, so if these results do not fit what the user meant, say so "
            "instead of forcing them.]")
WEB_CHECK = "[Source: a test search the app ran to check your web access.]"
WEATHER = ("[Source: the live weather service, fetched automatically because the "
           "user asked about the weather.]")
WEATHER_OWN = "[Source: your own live weather lookup (get_weather) - you chose to run it.]"
LINKS = "[Source: the pages from the links the user sent, opened just now.]"
REREAD = "[Source: a lookup you saved earlier, read again because you asked for it.]"

# The line under her reply (the page shows it with its own icon).
RECALLED_READS = "Recalled: "

# =============================================================================


def _join(items) -> str:
    items = [str(i) for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def turn_line(new_items=(), recalled_items=()) -> str:
    """The [Sources this turn: ...] line ("" when nothing is new or recalled).
    Items are names like "Picture 3" / "Document 2" / "Video 1"."""
    parts = []
    if new_items:
        parts.append(NEW_LINE.format(items=_join(new_items)))
    if recalled_items:
        one = len(list(recalled_items)) == 1
        parts.append(RECALLED_LINE.format(items=_join(recalled_items),
                                          them="it" if one else "them"))
    return (TURN_HEAD + " ".join(parts) + TURN_TAIL) if parts else ""


def item_name(row) -> str:
    """ "Picture 3" / "Document 2" / "Video 1" for an attachment row."""
    kind = {"document": "Document", "video": "Video"}.get(row.get("kind"), "Picture")
    return f"{kind} {row.get('chat_no') or row.get('id')}"


def tagged(tag: str, text: str) -> str:
    """A tool result with its source tag as the first line."""
    return tag + "\n" + text
