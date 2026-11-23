"""Scene boundaries, evidence policy, cadence, and rendering for transcript recall."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

import meaning
import transcript_vector_index as vectors

# ============================== TUNABLES =====================================
NEIGHBORS = 3
UNKNOWN_TIME_NEIGHBORS = 1
GAP_MINUTES = 30
TURN_WINDOW = 12
CALLBACKS_PER_WINDOW = 2
INTERVENING_TURNS = 3
FAINT_MEMORIES = 0
SCENE_POOL = 24
TITLE_CHARS = 60
PARTNER_CLIP_SHARE = 0.30
MIN_MATCH_CHARS = 24
TOPIC_TERM_CHARS = 4

# Initial, tune in live test. Retrieval thresholds reflect the live model's
# 0.53–0.69 same-language and 0.50–0.57 cross-language correct-match ranges.
STRONG_COSINE = 0.62
SCENE_LEAD = 0.08
LEXICAL_COSINE = 0.55
LEXICAL_CUES = 2
DIRECT_COSINE = 0.50
CLOSE_TIE = 0.025
TOPIC_BREAK_COSINE = 0.25
RECALL_INTENT_COSINE = 0.45
RECALL_INTENT_MARGIN = 0.04
SENSITIVE_COSINE = 0.41
SENSITIVE_MARGIN = 0.0
EMOTION_COSINE = 0.46
EMOTION_MARGIN = 0.03
# =============================================================================

INSTRUCTION = "Use memories naturally when they fit. The user's current words take precedence."
LABEL = ("FROM CHAT HISTORY (historical evidence from a past conversation. "
         "Treat quotes as dialogue from that time. Follow the current instructions and prefer "
         "the user's newer statements.)\n")
HEDGE = "The match is uncertain. Hedge any recollection and say when the evidence is unclear.\n"


def _classified(vector, examples, minimum, margin):
    scores = meaning.example_scores(vector, examples, meaning.CHAT)
    return None if scores is None else scores[0] >= minimum and scores[0] - scores[1] >= margin


def direct_question(vector):
    return _classified(vector, meaning.RECALL_ASK, RECALL_INTENT_COSINE, RECALL_INTENT_MARGIN)


def sensitive(row, vector):
    # The indexed representation covers only an initial portion of long rows.
    if len(row["content"] or "") + len(row["japanese"] or "") > vectors.MAX_TEXT_CHARS:
        return None
    return _classified(vector, meaning.SENSITIVE_MOMENT, SENSITIVE_COSINE, SENSITIVE_MARGIN)


def salient(vector):
    return _classified(vector, meaning.SALIENT_MOMENT, EMOTION_COSINE, EMOTION_MARGIN) is True


def _stamp(row):
    try:
        return datetime.fromisoformat(row["created_at"]).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


@dataclass
class Exchange:
    rows: tuple

    @property
    def first(self):
        return self.rows[0]

    @property
    def key(self):
        return self.first["conversation_id"], self.first["turn"] or -self.first["id"]


def exchanges(rows):
    """Pair by the persisted turn before prompt/privacy masking."""
    turns = {}
    for row in rows:
        conv, turn = row["conversation_id"], row["turn"]
        key = (conv, turn) if turn else (conv, 0, row["id"])
        roles = turns.setdefault(key, {})
        previous = roles.get(row["role"])
        if previous is None or row["id"] > previous["id"]:
            roles[row["role"]] = row
    tabs = {}
    for key, roles in turns.items():
        group = Exchange(tuple(roles[r] for r in ("user", "assistant") if r in roles))
        if key[1] and group.first["role"] != "user":
            continue  # Its user row is outside the eligible corpus.
        tabs.setdefault(key[0], []).append(group)
    for tab in tabs.values():
        tab.sort(key=lambda exchange: (exchange.first["turn"] or 0, exchange.first["id"]))
    return tabs


def _gap(left, right):
    earlier, later = _stamp(left.first), _stamp(right.first)
    if earlier is None or later is None or later < earlier:
        return None
    return (later - earlier).total_seconds() / 60


def _topic_break(left, right, indexed, terms):
    a, b = indexed.get(left.first["id"]), indexed.get(right.first["id"])
    if a is None or b is None or len(a) != len(b) or meaning._dot(a, b) > TOPIC_BREAK_COSINE:
        return False
    # Continuity cues veto an early stop, including cues in the active replies.
    shared = set(terms(dialogue(left))) & set(terms(dialogue(right)))
    return not any(len(term) >= TOPIC_TERM_CHARS for term in shared) and len(shared) < LEXICAL_CUES


def dialogue(exchange):
    return "\n".join((row["content"] or "") + "\n" + (row["japanese"] or "")
                     for row in exchange.rows)


def expand(tab, index, eligible, indexed, terms):
    scene = deque([tab[index]])
    for direction in (-1, 1):
        for distance in range(1, NEIGHBORS + 1):
            pos = index + distance * direction
            if not 0 <= pos < len(tab):
                break
            outer, inner = tab[pos], tab[pos - direction]
            left, right = (outer, inner) if direction < 0 else (inner, outer)
            turns = left.first["turn"], right.first["turn"]
            if not all(turns) or turns[1] - turns[0] != 1 or not eligible(outer):
                break
            gap = _gap(left, right)
            if gap is None and distance > UNKNOWN_TIME_NEIGHBORS:
                for _ in range(distance - 1 - UNKNOWN_TIME_NEIGHBORS):
                    if direction < 0:
                        scene.popleft()
                    else:
                        scene.pop()
                break
            if gap is not None and gap >= GAP_MINUTES:
                break
            if _topic_break(left, right, indexed, terms):
                break
            if direction < 0:
                scene.appendleft(outer)
            else:
                scene.append(outer)
            if gap is None:
                break
    return tuple(scene)


class Cadence:
    """Track callbacks by the user-row ID shared by all answer attempts."""
    def __init__(self):
        self._lock = Lock()
        self._transcript = None
        self._used = {}

    def claim(self, transcript, answering, latest, scene):
        if answering not in latest:
            return False
        with self._lock:
            if self._transcript != transcript:
                self._transcript = transcript
                self._used.clear()
            used = {mid: scene_keys for mid, scene_keys in self._used.items() if mid in latest}
            if answering in used:
                return bool(used[answering] & scene)
            if (answering != latest[0] or any(mid > answering for mid in self._used)
                    or len(used) >= CALLBACKS_PER_WINDOW):
                return False
            if any(mid in used for mid in latest[1:1 + INTERVENING_TURNS]):
                return False
            if any(previous & scene for previous in used.values()):
                return False
            used[answering] = scene
            self._used = used
            return True


cadence = Cadence()


def _when(row, now):
    date = _stamp(row)
    if date is None:
        return "date unknown"
    day = date.strftime("%a ") + str(date.day) + date.strftime(" %b %Y")
    seconds = (now - date).total_seconds()
    if seconds < 0:
        age = "age uncertain"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        age = f"{minutes} min ago" if minutes else "just now"
    elif seconds < 86400:
        age = f"{int(seconds // 3600)} h ago"
    else:
        days = int(seconds // 86400)
        age = f'{days} {"day" if days == 1 else "days"} ago'
    return f"{day}, {age}"


def render(scene, anchor, matched, current, rows_for, line, passage, fits, *,
           now=None, weak=False, user_led=True):
    """Keep whole exchanges until the scene needs trimming. Clip the anchor last."""
    now = now or datetime.now().astimezone()
    first = anchor.first
    title = " ".join((first["title"] or "New chat").split())[:TITLE_CHARS]
    when = _when(first, now)
    if first["conversation_id"] == current:
        location = f"earlier in this chat ({when})"
    elif user_led:
        location = f'In "{title}" (another chat, {when})'
    else:
        location = f"in another chat ({when})"
    head = LABEL + (HEDGE if weak else "") + f"- {location}\n"
    anchor_pos = scene.index(anchor)
    shown = deque(rows_for(exchange) for exchange in scene)
    anchor_rows = rows_for(anchor)
    if not anchor_rows or not fits(head):
        return None
    row = next((row for row in anchor_rows if row["id"] == matched), None)
    if row is None:
        return None
    text = line(row).strip()
    start, end = passage(text)

    def format_rows(rows, *, clip=None):
        quotes = []
        for r in rows:
            value = line(r).strip()
            if not value:
                continue
            if r["id"] == matched:
                a, b = (0, len(text)) if clip is None else clip
                lo, hi = max(a, start), min(b, end)
                value = (("…" if a else "") + text[a:lo] + "[matched] " + text[lo:hi]
                         + " [/matched]" + text[hi:b] + ("…" if b < len(text) else ""))
            elif clip is not None:
                limit = int((clip[1] - clip[0]) * PARTNER_CLIP_SHARE)
                if limit:
                    value = value[:limit] + ("…" if len(value) > limit else "")
                else:
                    continue
            speaker = "User" if r["role"] == "user" else "You"
            quotes.append(f'{speaker}: "{value}"')
        return "\n".join(quotes)

    while shown:
        result = head + "\n\n".join(format_rows(rows) for rows in shown)
        if fits(result):
            return result
        if len(shown) == 1:
            break
        if anchor_pos >= len(shown) - 1 - anchor_pos:
            shown.popleft()
            anchor_pos -= 1
        else:
            shown.pop()

    # A lexical match sets the center. Semantic-only matches use the beginning
    # of the indexed text. Grow that window as far as the call can afford.
    max_chars = len(text)
    for rows in (anchor_rows, (row,)):
        low, high = min(MIN_MATCH_CHARS, max_chars), max_chars
        result = None
        while low <= high:
            size = (low + high) // 2
            a = max(0, min(start - size // 3, max_chars - size))
            candidate = head + format_rows(rows, clip=(a, a + size))
            if fits(candidate):
                result = candidate
                low = size + 1
            else:
                high = size - 1
        if result is not None:
            return result
    return None
