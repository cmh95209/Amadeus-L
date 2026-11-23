"""Other-tab catch-up and transcript recall across all chats.

Two independent features, each with its own switch in Settings -> Preferences
(both ON by default; data/cross_tab.json):

1. CATCH-UP ("Remember my other chats") - catch_up_block():
   a short note on the user's other tabs, newest first, filled up to a token
   budget: the newest FULL_TABS tabs get their rolling summary + last lines,
   older ones a one-line mention. No day or tab limit - the budget decides.
   It changes only when another tab changes (dates, not "2 hours ago"), so it
   sits with her fixed blocks and keeps the prompt-cache discount.

2. SEARCH ("Search old chats") - ReplySearch:
   language-agnostic keyword and semantic ranking across all chats. Messages
   already in the reply are masked. One scene can go in its changing note.

Plus LEARN ON LEAVE (always on): when the user leaves a tab with enough new
messages, the memory service learns from it at once instead of waiting for
its usual 10 messages (cm_bridge.learn_now).

Every number worth tuning is in the TUNABLES block below. Neither feature
ever writes to the chat database; both fail closed (any error -> no block,
her prompt is simply without it).
"""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import memory as store
import meaning
import moment_recall as moment
import transcript_vector_index as vectors

# ============================== TUNABLES =====================================
CATCH_UP_BUDGETS = {"cloud": 1200, "local": 900, "builtin": 450}
STANDALONE_SEARCH_BUDGET = 1200
FULL_TABS = 3                  # newest tabs that get summary + last lines
MAX_TABS = 40                  # other tabs looked at, at most (the budget usually stops sooner)
SUMMARY_CHARS = 420            # a full tab's summary, at most
LAST_LINE_CHARS = 160          # each of its last lines, at most
LAST_LINES = 2                 # the user's last line + her reply to it
SHORT_CHARS = 110              # an older tab's one-line mention, at most

SEARCH_RESULTS = 3             # standalone ranked search results
SEARCH_RARE_SHARE = 0.01       # a query word in at most this share of exchanges is "rare" ...
SEARCH_RARE_MIN = 5            # ... (or in at most this many) - a match needs one
SEARCH_UNIQUE_HITS = 2         # a rare NAME this unique is a match on its own
SEARCH_TAIL_SHARE = 0.5        # other matches need at least this share of the best one's score
SEARCH_COMMON_SHARE = 0.08     # a term in more than this share of exchanges = stop word ...
SEARCH_COMMON_MIN = 12         # ... and in more than this many (small histories)
SEARCH_MAX_TERMS = 40          # query terms used at most
SEARCH_RANK_OFFSET = 5         # reciprocal-rank fusion balances keyword and semantic hits
SEARCH_RENDER_POOL = 128       # candidates rechecked against the transcript before rendering
PASSAGE_CHARS = 160
STEM_FROM = 5                  # words this long also match their other forms ...
STEM_MIN = 4                   # ... by their first letters: at least this many,
STEM_CUT = 2                   # ... the word minus this many end letters

LEARN_ON_LEAVE_MIN = 6         # new messages in the tab left (user + her) to learn now

DEFAULTS = {"catch_up": True, "search": True}
# Scripts written without spaces between words (Unicode character-name
# prefixes). Text in these is searched by 2-letter pieces; everything else by
# words. Add a script here to change how it is searched.
UNSPACED_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "THAI", "LAO",
                    "KHMER", "MYANMAR", "TIBETAN", "HALFWIDTH KATAKANA",
                    "IDEOGRAPHIC", "YI ", "BOPOMOFO")

CATCH_UP_LABEL = (
    "YOUR OTHER CHATS (what you and the user talked about in your OTHER chat "
    "tabs, newest first - you remember these yourself; bring them up only when "
    "it fits, and never mix them up with this chat; their files are not in front "
    "of you - describe only what is written here):\n")
SEARCH_LABEL = moment.LABEL
RECALL_INSTRUCTION = moment.INSTRUCTION
# =============================================================================

SETTINGS_FILE = "cross_tab.json"


# ---------------------------------------------------------------- settings --

def _settings_path() -> str:
    return os.path.join(store.DATA_DIR, SETTINGS_FILE)


def load_settings() -> Dict[str, bool]:
    """Both switches; a missing or broken file means the defaults."""
    out = dict(DEFAULTS)
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in DEFAULTS:
            if isinstance(data.get(key), bool):
                out[key] = data[key]
    except Exception:
        pass
    return out


def save_settings(changes: Dict[str, object]) -> Dict[str, bool]:
    """Change one or both switches (unknown keys / non-booleans: ValueError)."""
    current = load_settings()
    was_searching = current["search"]
    for key, value in (changes or {}).items():
        if key not in DEFAULTS or not isinstance(value, bool):
            raise ValueError(f"Unknown setting or not on/off: {key}")
        current[key] = value
    path = _settings_path()
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f)
    os.replace(tmp, path)
    if current["search"] and not was_searching:
        vectors.search_reenabled(store.PATH_TO_MEMORY)
    return current


def catch_up_on() -> bool:
    return load_settings()["catch_up"]


def search_on() -> bool:
    return load_settings()["search"]


# ------------------------------------------------------------------ budget --

def _brain() -> str:
    try:
        import builtin_llm
        if builtin_llm.active():
            return "builtin"
        import llm
        return "local" if llm._is_local_host(llm._configured_server_url()) else "cloud"
    except Exception:
        return "cloud"


def reserve_tokens() -> int:
    """Room the history gives up for these blocks - built-in model only (its
    context window is fixed and small; other brains have room to spare)."""
    if _brain() != "builtin":
        return 0
    return CATCH_UP_BUDGETS["builtin"] if catch_up_on() else 0


def _tokens(text: str) -> int:
    return store.estimate_tokens(text)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _day(stamp: str) -> str:
    """'2026-11-12 18:30' -> 'Thu 12 Nov 2026' (a date, so the text stays the
    same all day - the prompt cache needs it unchanged)."""
    try:
        d = datetime.fromisoformat(str(stamp))
        return d.strftime("%a ") + str(d.day) + d.strftime(" %b %Y")
    except (TypeError, ValueError):
        return str(stamp or "")[:10]


# ---------------------------------------------------------------- catch-up --

def _her_line(row) -> str:
    # Her lines as the chat history shows them to her: the spoken (Japanese)
    # line when there is one.
    return (row["japanese"] or row["content"] or "") if row["role"] == "assistant" else (row["content"] or "")


def _have_chats() -> bool:
    """Never create a chat database just to read it (a fresh install, or a
    test without one)."""
    return os.path.exists(store.PATH_TO_MEMORY)


def _dicts(cursor, sql: str, params=()) -> List[dict]:
    """Run one query; rows as dicts by column name (the store's connections
    are wrapped, so a row_factory cannot be set on them)."""
    result = cursor.execute(sql, params)
    names = [d[0] for d in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


def _other_tabs(current: int) -> List[Dict[str, object]]:
    """Other tabs with at least one user message, newest activity first."""
    if not _have_chats():
        return []
    conn = store._connect_db()
    try:
        c = conn.cursor()
        tabs = _dicts(c,
            "SELECT c.id, c.title, COALESCE(c.rolling_summary, '') AS summary, "
            "       MAX(m.created_at) AS last_at, MAX(m.id) AS last_id "
            "FROM conversations c JOIN messages m ON m.conversation_id = c.id "
            "WHERE c.id != ? AND COALESCE(m.active, 1) = 1 "
            "GROUP BY c.id HAVING SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) > 0 "
            "ORDER BY last_at DESC, last_id DESC", (int(current),))
        tabs = tabs[:MAX_TABS]
        for tab in tabs:
            last = _dicts(c,
                "SELECT role, content, japanese FROM messages "
                "WHERE conversation_id = ? AND COALESCE(active, 1) = 1 "
                "AND COALESCE(aside, 0) = 0 AND role IN ('user', 'assistant') "
                "ORDER BY id DESC LIMIT 6", (tab["id"],))
            lines, seen_user = [], False
            for r in last:                      # newest first
                if r["role"] == "user":
                    lines.append(("User", r["content"] or ""))
                    seen_user = True
                    break
                if not lines:
                    lines.append(("You", _her_line(r)))
            tab["last"] = list(reversed(lines)) if seen_user else lines
    finally:
        conn.close()
    return tabs


def _short(head: str, tab: dict) -> str:
    """An older tab in one line: its summary, else the user's last line."""
    gist = tab["summary"] or next((text for who, text in tab.get("last", []) if who == "User"), "")
    return head + (": " + _clip(gist, SHORT_CHARS) if gist.strip() else "")


def catch_up_block(conversation_id=None, budget: Optional[int] = None) -> Optional[dict]:
    """The catch-up note as one system message, or None (switch off, no other
    tabs, or any error)."""
    try:
        if not catch_up_on():
            return None
        current = store.load_active_conversation() if conversation_id is None else int(conversation_id)
        budget = CATCH_UP_BUDGETS[_brain()] if budget is None else budget
        tabs = _other_tabs(current)
        if not tabs:
            return None
        parts, used = [], _tokens(CATCH_UP_LABEL)
        for i, tab in enumerate(tabs):
            title = _clip(str(tab["title"] or "New chat"), 60)
            head = f'- "{title}" (last talked {_day(tab["last_at"])})'
            if i < FULL_TABS:
                bits = []
                if tab["summary"].strip():
                    bits.append(_clip(tab["summary"], SUMMARY_CHARS))
                last = "; ".join(f'{who}: "{_clip(text, LAST_LINE_CHARS)}"'
                                 for who, text in tab.get("last", [])[-LAST_LINES:] if text.strip())
                if last:
                    bits.append("Last: " + last)
                line = head + ": " + " ".join(bits) if bits else head
            else:
                line = _short(head, tab)
            cost = _tokens(line) + 1
            if used + cost > budget:
                if i < FULL_TABS:           # a full one does not fit: try it short
                    line = _short(head, tab)
                    cost = _tokens(line) + 1
                if used + cost > budget:
                    break
            parts.append(line)
            used += cost
        if not parts:
            return None
        return {"role": "system", "content": CATCH_UP_LABEL + "\n".join(parts)}
    except Exception as exc:
        print(f"[Amadeus] cross-tab catch-up skipped: {exc!r}")
        return None


# ------------------------------------------------------------------ search --

_RUN = re.compile(r"\w+", re.UNICODE)
_unspaced_cache: Dict[str, bool] = {}


def _normal(text: str) -> str:
    """Same form for the query and the messages: NFKC (full-width -> normal,
    compatibility forms folded) + casefold (case-insensitive in every script)."""
    return unicodedata.normalize("NFKC", text or "").casefold()


def _unspaced(ch: str) -> bool:
    hit = _unspaced_cache.get(ch)
    if hit is None:
        name = unicodedata.name(ch, "")
        hit = name.startswith(UNSPACED_SCRIPTS)
        _unspaced_cache[ch] = hit
    return hit


def query_terms(text: str) -> List[str]:
    """Search terms, in any language: whole words where the script uses
    spaces, 2-letter pieces where it does not (a run mixing both is split at
    the change). Single letters are dropped."""
    terms: List[str] = []
    for run in _RUN.findall(_normal(text)):
        piece, kind = "", None
        for ch in run + "\0":
            k = None if ch == "\0" else _unspaced(ch)
            if k != kind and piece:
                if kind:
                    terms.extend(piece[i:i + 2] for i in range(len(piece) - 1))
                elif len(piece) >= 2:
                    terms.append(piece)
                piece = ""
            if ch != "\0":
                piece += ch
            kind = k
    out, seen = [], set()
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:SEARCH_MAX_TERMS]


def name_terms(text: str) -> set:
    """Words that look like names in the user's own writing: capitalised
    after the start of a sentence, or holding a digit (Gaviscon, Genshin,
    RTX5090). Only cased scripts have capitals, so elsewhere this is empty -
    those words then need a second matching word like any other."""
    out = set()
    text = unicodedata.normalize("NFKC", text or "")
    for m in _RUN.finditer(text):
        word = m.group()
        before = text[:m.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?。！？\n"
        if any(ch.isdigit() for ch in word) or (word[:1].isupper() and not sentence_start):
            out.add(word.casefold())
    return out


def _matcher(term: str):
    """A test for one term. 2-letter pieces (unspaced scripts) match anywhere.
    Words match at the START of a word, and a word of STEM_FROM+ letters
    also matches its other forms by its first letters (user/users,
    suggest/suggestion, plan/planned) - a crude, language-neutral stemmer;
    shorter words must match whole ('cat' never hits 'category')."""
    if any(_unspaced(ch) for ch in term):
        return lambda text: term in text
    if len(term) >= STEM_FROM:
        stem = term[:max(STEM_MIN, len(term) - STEM_CUT)]
        pattern = re.compile(r"(?<!\w)" + re.escape(stem))
        return lambda text: stem in text and pattern.search(text) is not None
    pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")
    return lambda text: term in text and pattern.search(text) is not None


def _candidates(ids=None) -> List[dict]:
    if (ids is not None and not ids) or not _have_chats():
        return []
    conn = store._connect_db()
    try:
        return vectors.source_rows(conn, ids)
    finally:
        conn.close()


def _keyword_ranks(query, units):
    terms = query_terms(query)
    if not terms or not units:
        return [], {}, set()
    n = len(units)
    tests = {t: _matcher(t) for t in terms}
    hits = {t: [i for i, u in enumerate(units) if tests[t](u["text"])] for t in terms}
    weights = {}
    for t, where in hits.items():
        df = len(where)
        if df == 0 or df > max(SEARCH_COMMON_MIN, SEARCH_COMMON_SHARE * n):
            continue                     # never used before, or a stop word here
        weights[t] = math.log((n + 1) / (df + 0.5))
    if not weights:
        return [], {}, set()
    # The topic is carried by the RARE words of the query (rare in this
    # user's own history - so it works the same in every language). A match
    # must hold one of them, plus a second query word unless that rare word
    # is a name (name_terms) found almost nowhere else; the best match sets
    # the bar for the rest.
    rare_cut = max(SEARCH_RARE_MIN, SEARCH_RARE_SHARE * n)
    names = name_terms(query)
    rare = {t for t in weights if len(hits[t]) <= rare_cut}
    if not rare:
        return [], {}, set()
    score: Dict[int, float] = {}
    cues: Dict[int, set] = {}
    unique: Dict[int, bool] = {}
    for t, w in weights.items():
        for i in hits[t]:
            score[i] = score.get(i, 0.0) + w
            cues.setdefault(i, set()).add(t)
            if t in rare and t in names and len(hits[t]) <= SEARCH_UNIQUE_HITS:
                unique[i] = True
    holders = {i for t in rare for i in hits[t]}
    good = [i for i in holders if _distinct_cues(cues[i]) or unique.get(i)]
    if not good:
        return [], cues, rare
    best = max(score[i] for i in good)
    ranked = sorted((i for i in good if score[i] >= SEARCH_TAIL_SHARE * best),
                    key=lambda i: (score[i], units[i]["first"]["id"]), reverse=True)
    return ranked, cues, rare


def _distinct_cues(cues):
    # Adjacent bigrams from one three-character fragment only count once.
    return len(cues) >= moment.LEXICAL_CUES and any(
        not (_unspaced(a[0]) and _unspaced(b[0]) and (a[-1] == b[0] or b[-1] == a[0]))
        for a in cues for b in cues if a < b
    )


def _passage(text, terms):
    # Preserve original offsets while scoring language-neutral word/bigram cues.
    if len(text) <= PASSAGE_CHARS:
        return 0, len(text)
    tests = [(term, _matcher(term)) for term in terms]
    spans = list(re.finditer(r"[^.!?。！？\n]+[.!?。！？]*", text))
    if not spans or not tests:
        return 0, min(len(text), PASSAGE_CHARS)
    best, location = 0, 0
    for span in spans:
        value = span.group()
        for start in range(0, len(value), PASSAGE_CHARS // 2):
            area = _normal(value[start:start + PASSAGE_CHARS])
            matches = [term for term, test in tests if test(area)]
            if len(matches) > best:
                offsets = [area.find(term) for term in matches if term in area]
                best = len(matches)
                location = min(len(text) - 1, span.start() + start + min(offsets, default=0))
    return location, min(len(text), location + PASSAGE_CHARS)


def _quote_key(text):
    return " ".join(_RUN.findall(_normal(text)))


def _prompt_texts(messages):
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            yield from (p["text"] for p in content
                        if isinstance(p, dict) and isinstance(p.get("text"), str))


def _latest_users():
    conn = store._connect_db()
    try:
        return tuple(row[0] for row in conn.execute(
            "SELECT id FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT ?",
            (moment.TURN_WINDOW,),
        ))
    finally:
        conn.close()


@dataclass
class _Anchor:
    tab: list
    index: int
    rows: tuple
    matched: int
    cosine: float
    score: float
    keyword: bool
    cues: set
    rare_cues: set

    @property
    def exchange(self):
        return self.tab[self.index]


class ReplySearch:
    """A reply's transcript snapshot and one semantic query, shared by retries."""

    def __init__(self, query, conversation_id, *, excluded_ids=(), answering_id=None, now=None):
        self.query = query
        self.current = conversation_id
        self.excluded = set(excluded_ids)
        self.answering_id = answering_id
        self.now = now
        self.tabs = None
        self.semantic = None
        self.ranks = {}

    def prefetch(self):
        if self.answering_id is not None:
            self.semantic = vectors.begin_query(self.query, frozenset(), store.PATH_TO_MEMORY,
                                                prefetch=True)

    def _rank(self, verbatim_message_ids):
        visible = frozenset(verbatim_message_ids)
        if visible in self.ranks:
            return self.ranks[visible]
        if self.tabs is None:
            rows = _candidates()
            if self.answering_id is not None:
                self.excluded.update(r["id"] for r in rows if r["id"] >= self.answering_id)
            self.tabs = moment.exchanges(rows)
            if self.semantic is None:
                self.semantic = vectors.begin_query(self.query, self.excluded | visible, store.PATH_TO_MEMORY)
        masked = self.excluded | visible
        matches = self.semantic.hits(masked) if self.semantic else {}
        units, positions = [], []
        for tab in self.tabs.values():
            for index, exchange in enumerate(tab):
                rows = tuple(row for row in exchange.rows if row["id"] not in masked)
                if rows:
                    text = "\n".join((row["content"] or "") + "\n" + (row["japanese"] or "")
                                     for row in rows)
                    units.append({"first": rows[0], "rows": rows, "text": _normal(text)})
                    positions.append((tab, index))
        keyword, cues, rare = _keyword_ranks(self.query, units)
        keyword_set = set(keyword)
        semantic = {}
        all_scores = {}
        for i, unit in enumerate(units):
            for row in unit["rows"]:
                hit = matches.get(row["id"])
                if hit and hit[0] == vectors.content_hash(row):
                    cosine = hit[1]
                    all_scores[row["id"]] = cosine
                    if cosine > semantic.get(i, (-1.0, 0))[0]:
                        semantic[i] = (cosine, row["id"])
        semantic_rank = sorted(
            (i for i in semantic if semantic[i][0] >= moment.DIRECT_COSINE),
            key=lambda i: (semantic[i][0], units[i]["first"]["id"]), reverse=True)
        fused = {}
        for ranking in (keyword, semantic_rank):
            for rank, i in enumerate(ranking, start=1):
                fused[i] = fused.get(i, 0) + 1 / (SEARCH_RANK_OFFSET + rank)
        order = sorted(fused, key=lambda i: (fused[i], units[i]["first"]["id"]), reverse=True)
        ranked = []
        for i in order[:SEARCH_RENDER_POOL]:
            tab, index = positions[i]
            rows = units[i]["rows"]
            matched = semantic.get(i, (0.0, 0))[1]
            lexical = cues.get(i, set())
            if i in keyword_set and semantic.get(i, (0.0,))[0] < moment.DIRECT_COSINE:
                tests = [_matcher(t) for t in lexical]
                matched = max(rows, key=lambda row: sum(test(_normal(_her_line(row))) for test in tests))["id"]
            ranked.append(_Anchor(tab, index, rows, matched, semantic.get(i, (0.0,))[0],
                                  fused[i], i in keyword_set, lexical, lexical & rare))
        self.ranks[visible] = ranked, all_scores
        return ranked, all_scores

    def _scenes(self, verbatim_message_ids, prompt_messages, direct):
        anchors, scores = self._rank(verbatim_message_ids)
        if not anchors:
            return
        embedded = getattr(self.semantic, "embedded", None)
        fingerprint, vector = embedded if embedded else (None, meaning.cached_vector(self.query))
        if direct is None:
            direct = moment.direct_question(vector) is True
        if not direct and self.answering_id is None:
            return

        masked = self.excluded | set(verbatim_message_ids)
        prompt = [" " + _quote_key(text) + " " for text in _prompt_texts(prompt_messages)]
        nearby = {}
        for candidate in anchors[:moment.SCENE_POOL]:
            lo = max(0, candidate.index - moment.NEIGHBORS)
            hi = candidate.index + moment.NEIGHBORS + 1
            nearby.update((row["id"], row) for exchange in candidate.tab[lo:hi] for row in exchange.rows)
        fresh = {r["id"]: r for r in _candidates(tuple(nearby))}
        indexed = vectors.scene_vectors(store.PATH_TO_MEMORY, list(fresh.values()), fingerprint)
        terms = query_terms(self.query)
        term_set = set(terms)
        latest = _latest_users() if not direct else ()
        safe = {}
        shown = {}

        def rows_for(exchange):
            mid = exchange.first["id"]
            if mid in shown:
                return shown[mid]
            rows = []
            seen = set()
            for row in exchange.rows:
                text = _her_line(row)
                key = _quote_key(text)
                if row["id"] in masked or not key or key in seen:
                    continue
                clips = [key, *(_quote_key(_clip(text, n)) for n in (LAST_LINE_CHARS, SHORT_CHARS))]
                if any(f" {clip} " in p for p in prompt for clip in clips if clip):
                    continue
                seen.add(key)
                rows.append(row)
            shown[mid] = tuple(rows)
            return shown[mid]

        def eligible(exchange, *, anchor=False):
            if any(row["id"] in self.excluded or fresh.get(row["id"]) != row for row in exchange.rows):
                return False
            rows = rows_for(exchange)
            if not rows:
                return False
            if exchange.first["id"] not in safe:
                safe[exchange.first["id"]] = all(
                    moment.sensitive(row, indexed.get(row["id"])) is False for row in exchange.rows)
            if safe[exchange.first["id"]]:
                return True
            if not direct:
                return False
            # The user invited this exchange. Adjacent sensitive or unknown
            # material needs its own connection to their request.
            return anchor or _distinct_cues(term_set & set(query_terms(moment.dialogue(exchange)))) or any(
                scores.get(row["id"], 0.0) >= moment.LEXICAL_COSINE for row in rows)

        choices = []
        for candidate in anchors[:moment.SCENE_POOL]:
            anchor = candidate.exchange
            if not eligible(anchor, anchor=True) or all(r["id"] != candidate.matched for r in rows_for(anchor)):
                continue
            scene = moment.expand(candidate.tab, candidate.index, eligible, indexed, query_terms)
            scene_rows = {row["id"] for exchange in scene for row in exchange.rows}
            rival = max((cosine for mid, cosine in scores.items() if mid not in scene_rows), default=0.0)
            lexical = bool(candidate.rare_cues and _distinct_cues(candidate.cues))
            strong = ((candidate.cosine >= moment.STRONG_COSINE
                       and candidate.cosine >= rival + moment.SCENE_LEAD)
                      or (candidate.cosine >= moment.LEXICAL_COSINE and lexical))
            if direct or strong or (moment.FAINT_MEMORIES and candidate.keyword):
                choices.append((candidate, scene, not strong))
        if not choices:
            return

        best = choices[0][0]

        def order(choice):
            candidate, _, _ = choice
            first = candidate.exchange.first
            shared = candidate.cues & best.cues
            repeated = (first["role"] == best.exchange.first["role"] == "user"
                        and bool(best.rare_cues) and best.rare_cues <= shared and _distinct_cues(shared))
            tied = (best.score - candidate.score <= moment.CLOSE_TIE
                    and best.cosine - candidate.cosine <= moment.CLOSE_TIE)
            if repeated:
                return (2, first["id"], 0)
            if tied:
                return (1, moment.salient(indexed.get(first["id"])), first["id"])
            return (0, candidate.score, first["id"])

        for candidate, scene, weak in sorted(choices, key=order, reverse=True):
            passage_terms = candidate.rare_cues or terms
            yield (candidate, scene, rows_for, lambda text: _passage(text, passage_terms),
                   direct, latest, weak)

    def block(self, verbatim_message_ids=(), prompt_messages=(), *, budget=None, fits=None,
              count=None, direct=None):
        """Render one fresh scene inside the actual call's remaining room."""
        try:
            if not search_on():
                return None
            budget = STANDALONE_SEARCH_BUDGET if budget is None else budget
            count = count or _tokens

            def within_budget(text):
                return count(text) <= budget and (fits is None or fits(text))

            if not within_budget(SEARCH_LABEL):
                return None
            for anchor, scene, rows_for, passage, user_led, latest, weak in self._scenes(
                    verbatim_message_ids, prompt_messages, direct):
                rendered = moment.render(scene, anchor.exchange, anchor.matched, self.current,
                                         rows_for, _her_line, passage, within_budget,
                                         now=self.now, weak=weak, user_led=user_led)
                if not rendered:
                    continue
                expected = {row["id"]: row for exchange in scene for row in exchange.rows}
                if not search_on() or {row["id"]: row for row in _candidates(tuple(expected))} != expected:
                    continue
                keys = frozenset(exchange.key for exchange in scene)
                if user_led or moment.cadence.claim(store.PATH_TO_MEMORY, self.answering_id, latest, keys):
                    return {"role": "system", "content": rendered}
        except Exception as exc:
            print(f"[Amadeus] transcript recall skipped: {type(exc).__name__}")
        return None


def prepare_search(query, conversation_id, **kwargs):
    if isinstance(query, str) and query.strip() and search_on():
        current = store.load_active_conversation() if conversation_id is None else int(conversation_id)
        return ReplySearch(query, current, **kwargs)
    return None


def search(query, conversation_id=None, limit=SEARCH_RESULTS, *, verbatim_message_ids=(), excluded_ids=()):
    """Rank exchanges for an explicit transcript search."""
    reply = prepare_search(query, conversation_id, excluded_ids=excluded_ids)
    if reply is None or limit <= 0:
        return []
    ranked, _ = reply._rank(verbatim_message_ids)
    fresh = {row["id"]: row for row in _candidates([r["id"] for candidate in ranked for r in candidate.rows])}
    found = []
    for candidate in ranked:
        if any(fresh.get(row["id"]) != row for row in candidate.rows):
            continue
        first = candidate.rows[0]
        quotes = {row["role"]: _her_line(row) for row in candidate.rows}
        found.append({"conversation_id": first["conversation_id"], "title": first["title"],
                      "created_at": first["created_at"], "id": first["id"], "score": candidate.score,
                      "user": quotes.get("user", ""), "her": quotes.get("assistant", "")})
        if len(found) >= limit:
            break
    return found


def search_block(query, conversation_id=None, budget=None, *, verbatim_message_ids=(),
                 excluded_ids=(), prompt_messages=()):
    """Render a scene in response to an explicit search request."""
    reply = prepare_search(query, conversation_id, excluded_ids=excluded_ids)
    return reply.block(verbatim_message_ids, prompt_messages, budget=budget, direct=True) if reply else None


# --------------------------------------------------------- learn on leave --

def on_tab_left(previous_id, new_id) -> None:
    """The user switched tabs: ask the memory service to learn from the tab
    they left now (it skips it when fewer than LEARN_ON_LEAVE_MIN new
    messages wait there). Fire-and-forget; never raises."""
    try:
        if previous_id is None or int(previous_id) == int(new_id):
            return
        import cm_bridge
        cm_bridge.learn_now(int(previous_id), min_new=LEARN_ON_LEAVE_MIN)
    except Exception as exc:
        print(f"[Amadeus] learn-on-leave skipped: {exc!r}")
