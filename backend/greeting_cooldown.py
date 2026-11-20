"""Greeting cooldown: reopening the app right after she greeted you should
not make her greet you again.

If the page is opened, refreshed or reconnected within N seconds of her last
startup greeting in the same conversation - and the user has written nothing
since - NO new greeting is generated: she keeps the line that is already on
screen. (Seen live: two tabs, a phone and a reconnect all hit /greet within
the same few seconds, and the extra greetings made her next one think she
had been ignored.) The window is user-tunable in Settings -> Preferences
(0 = off: always greet, exactly the old behaviour); the value lives in a
plain text file next to memory.db, like attach_reaction.txt.

The gate keeps two /greet requests arriving at the same moment from BOTH
generating: the route holds it across the whole check -> generate -> note
sequence, so the second request waits for the first, then sees the cooldown
and stays quiet.
"""
import os
import threading
import time

import memory as store

# ---------- tunables (every knob of this feature lives here) ----------
DEFAULT_SECONDS = 15                 # the window when no setting file exists
CHOICES = (0, 15, 30, 60, 120, 300)  # 0 = off: always greet
FILE_NAME = "greeting_cooldown.txt"  # stored next to attach_reaction.txt

# Reentrant (not a plain Lock): the /greet route holds the gate across the
# check -> generate -> note sequence, so the recent/note calls it makes
# while holding it must not deadlock on themselves. Across threads it
# behaves exactly like a plain lock.
gate = threading.RLock()

# conversation id -> (time.monotonic() when she spoke, id of the stored
# line). In memory only - a backend restart clears it (after a restart she
# says she is back, which is a fresh moment worth a new line anyway).
_NOTED = {}


def setting_path() -> str:
    """The setting file, in the same folder as attach_reaction.txt (the
    data folder next to memory.db)."""
    return os.path.join(os.path.dirname(os.path.abspath(store.PATH_TO_MEMORY)),
                        FILE_NAME)


def load_seconds() -> int:
    """The cooldown window in seconds; DEFAULT_SECONDS when the file is
    missing or holds something invalid."""
    try:
        with open(setting_path(), encoding="utf-8") as f:
            value = int(f.read().strip())
    except (OSError, ValueError):
        return DEFAULT_SECONDS
    return value if value in CHOICES else DEFAULT_SECONDS


def save_seconds(value) -> int:
    """Persist a window from CHOICES (ValueError otherwise) and return it.
    Accepts an int (or an integral number like 30.0); anything else - a
    fraction, a string, a non-numeric value - is refused."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValueError("Unknown choice")
    if seconds != value or seconds not in CHOICES:
        raise ValueError("Unknown choice")
    with open(setting_path(), "w", encoding="utf-8") as f:
        f.write(str(seconds))
    return seconds


def note_greeting(conversation_id: int, message_id: int) -> None:
    """Remember (in memory, thread-safe) that she just greeted this
    conversation with the line stored under message_id."""
    with gate:
        _NOTED[int(conversation_id)] = (time.monotonic(), int(message_id))


def recent_greeting(conversation_id: int) -> int | None:
    """The id of her recent startup greeting in this conversation when a new
    one must be skipped, else None:

      * the window (the current setting) is 0 = off, or
      * nothing was noted for this conversation, or
      * the window has elapsed since the note, or
      * the stored line is gone (deleted in the meantime), or
      * the user has written since it (the conversation is already going).
    """
    seconds = load_seconds()
    if seconds == 0:
        return None
    with gate:
        noted = _NOTED.get(int(conversation_id))
    if noted is None:
        return None
    when, message_id = noted
    if time.monotonic() - when >= seconds:
        return None
    if not store.message_row_exists(message_id):
        return None
    if store.latest_user_message_id(conversation_id) > message_id:
        return None
    return message_id
