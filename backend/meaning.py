"""Compare meaning: is this message a weather question, does her reply state a
forecast, is the user talking about a file they sent in this chat?
(MH 2026-11-13.)

Uses the memory sidecar's embedding model (Qwen3-Embedding-0.6B, already
loaded for long-term memory - never a second copy) through its /embed
service. It works by MEANING, so it works in any language the model knows
(100+): no per-language word lists. The example sentences below are in
English on purpose; tests check other languages against them.

Every check returns None when the sidecar is off, still loading, slow
(TIMEOUT) or this module was never started: the caller then keeps its old
word rules. Nothing here changes her prompt - it only decides.

The example vectors are fetched once in the background (start(), called by
main.py); tests never call start(), so the app's tests never reach the
sidecar (they inject a fake embedder instead).
"""

import json
import os
import threading
import time
import urllib.request

# =============================================================================
# TUNABLES
# =============================================================================

ENABLED = True
SIDECAR_URL = os.environ.get("CM_SIDECAR_URL", "http://127.0.0.1:9870").rstrip("/")
TIMEOUT = 1.5              # seconds for one message; slower -> word rules
WARM_TIMEOUT = 30.0        # the one-off example fetch (in the background)
WARM_RETRY_SECONDS = 60    # sidecar not ready yet -> try again after this

# Recall by meaning (attachments.py): ADD brings back the latest file the
# user seems to mean ("the vid i sent"); BLOCK drops a file that matched on
# a word but not in meaning. Either can be switched off on its own.
RECALL_ADD = True
RECALL_BLOCK = True
RECALL_ADD_RECENT_HOURS = 48   # ADD only picks a file sent this recently
RECALL_OFF_TOPIC_LIMIT = 10    # a recalled file stays while the talk comes back to it; let go after
                               # this many messages in a row about something else

# A text "is" a group when its best match there is at least MIN and beats
# its best match in the opposite group by at least MARGIN. Measured
# 2026-11-13 (16 languages: en ja zh ms ta hi ko es fr de pt id vi th ru ar,
# casual texting, examples English only):
# - weather questions: 60/64 caught, 3/64 ordinary chat sent to the (cheap,
#   deciding) weather check; misses: es/fr/pt "tiempo/temps/tempo" (also
#   "time") and one Hindi - she still has her get_weather tool for those;
# - her reply states a forecast: 31/32 caught, 0/32 false;
# - "about a file I sent": 41/48 caught, 0/64 other videos/photos added
#   (one Tamil "file" read as a video);
# - BLOCK: 7/8 wrong word-rule recalls dropped, 0/20 real ones (the 8th,
#   2026-11-13's, is dropped by FIT_MIN: 0.18).
WEATHER_MIN, WEATHER_MARGIN = 0.35, 0.03
FORECAST_MIN, FORECAST_MARGIN = 0.45, 0.02
FILE_MIN, FILE_MARGIN = 0.50, 0.05
CLAIM_MIN, CLAIM_MARGIN = 0.40, 0.04   # lookup claims: 32/32 caught, 0/32 data talk
BLOCK_BELOW = -0.06        # file-talk margin at or under this = "other media"
LOOK_MIN, LOOK_MARGIN = 0.45, 0.06     # 7/7 look-again lines, 0/6 remember-lines (2026-11-13)
# Which file a message means (attachments._meaning_pick): its fit to each
# file's description must be at least PICK_MIN and beat the next file by
# PICK_MARGIN (PICK_MARGIN_TALK when the message is clearly about a file they
# sent). Measured 2026-11-13 on 102 references + 603 other lines, 18 chats:
# 70/102 right, 0 wrong files, 9/603 other lines (mostly real references).
PICK_MIN, PICK_MARGIN, PICK_MARGIN_TALK = 0.45, 0.12, 0.05
# Asks (SEARCH_ASK ...): measured 2026-11-19 with scripts/measure_asks.py
# (16 languages, casual texting, English examples): search 28/32 caught,
# 2/64 ordinary chat (a wrong yes only costs the deciding triage call);
# link 16/16, 1/64; again 13/16, 0/64; time 15/16 at margin 0.05 but 1/64
# false - TIME uses margin 0.10 (12/16, 0/64), since a "time" query skips
# a search (a miss only means the old behaviour: it is searched).
ASK_SEARCH_MIN = ASK_TIME_MIN = ASK_LINK_MIN = ASK_AGAIN_MIN = 0.50
ASK_MARGIN = 0.05
ASK_TIME_MARGIN = 0.10
FIT_MIN = 0.30             # message vs a file's description, below = blocked
                           # (only clear mismatches: talk that merely shares a
                           # topic scores 0.6 - other_media() handles that)

WEATHER_ASK = [
    "What's the weather like today?", "Is it going to rain later?",
    "Will it rain tomorrow?", "Should I bring an umbrella?",
    "Do I need a jacket tonight?", "How hot is it outside right now?",
    "Is it cold out there?", "What's the forecast for this weekend?",
    "Any chance of snow tomorrow?", "Is there a storm coming tonight?",
    "How humid is it today?", "What's the temperature in Osaka right now?",
    "Is it nice out today?", "Will it be sunny for the picnic on Saturday?",
    "Is the air quality bad today?", "gonna rain tmrw?", "how's the weather looking",
    "Is it raining there right now?", "Do I need sunscreen today?",
    "Should I wear shorts or a coat tomorrow?",
]
CHAT = [
    "I'm so tired today.", "What should I eat for dinner?", "I watched a movie last night.",
    "How was your day?", "I'm heading out to the mall.", "Work was so stressful.",
    "I can't sleep.", "Do you like cats?", "I need to finish my report.",
    "Let's play a game.", "I miss you.", "Tell me a story.", "My phone battery died.",
    "I'm going on a trip next week.", "What time is it in Tokyo?", "I'm feeling sick.",
    "I just got home.", "lol that's funny", "I took a walk in the park.",
    "Can you help me with my homework?",
]
FORECAST = [
    "It's 18 degrees and cloudy right now.", "There's an 80% chance of rain this afternoon.",
    "Thunderstorms are expected tonight, so stay inside.",
    "It will be sunny tomorrow with a high of 25.",
    "Light rain is forecast for the evening, bring an umbrella.",
    "It's currently snowing there.", "Tomorrow looks clear and warm.",
    "The humidity is around 70% today.", "Expect strong winds and showers later.",
    "The air quality is poor today.", "It's going to be hot this weekend, around 33 degrees.",
]
NOT_FORECAST = [
    "Which town do you mean? I'll check the forecast.", "I haven't looked up the weather yet.",
    "I can't know the weather without checking.", "I hope it doesn't rain on your trip.",
    "Stay warm and get some rest.", "Rainy days make me want to stay inside and read.",
    "You should sleep earlier tonight.", "That movie sounds interesting.",
    "I don't have live weather data right now.", "Tell me where you are and I'll look it up.",
    "I love the sound of rain.", "Good morning! Did you sleep well?",
]
# Her reply CLAIMS a lookup (chat._pretend_lookup_net) vs her in-character
# talk about data, memory and reasoning (2026-11-13: "things don't go
# according to the data" was taken for a search claim).
LOOKUP_CLAIM = [
    "I searched it up and found that the store opens at nine.",
    "According to my search, the movie comes out next month.",
    "I just looked it up online.", "Based on the latest data I found online, prices went up.",
    "The search results say it was cancelled.", "I checked the news and it says the game was delayed.",
    "I looked it up: the museum is closed on Mondays.",
    "According to the latest reports online, the event is sold out.",
    "I found on the internet that it's a common problem.", "My search shows three results.",
    "I just checked the website and the price is 20 dollars.",
]
NOT_LOOKUP_CLAIM = [
    "Things don't always go according to the data.", "The data doesn't lie.",
    "Statistically speaking, that's unlikely.", "From what I remember, you told me that last week.",
    "I looked up at the sky and thought about it.", "Based on our earlier conversations, you prefer tea.",
    "In my experience, that rarely works.", "Let me think about it logically.",
    "As a scientist, I'd want more data before concluding anything.",
    "I read about that in a paper years ago.", "You should look it up yourself sometime.",
    "I don't have that information - I'd need to search for it.",
    "Was it the lighting, the framing, or something else entirely?",
    "Looking at the chart you sent, your steps went up every month.",
    "I checked the file you sent earlier: the rent is 1,800.",
    "I read your document, and the plan looks tight.",
    "From the picture you showed me, it's an orange cat.",
    "In the recipe you gave me, the eggs boil for six and a half minutes.",
    "I already looked at your budget when you sent it.",
]

# The user asks her to really LOOK at / read a file again (cross_files.py
# brings the actual file only then; otherwise just what she noted about it).
LOOK_AGAIN = [
    "Can you see the picture right now?", "Look at it again.", "Open the file again and check.",
    "What's in the bottom corner of that photo?", "Read the document again for me.",
    "Zoom in on the chart, what's the number for May?", "Can you look at the pic one more time?",
]
NOT_LOOK_AGAIN = [
    "Do you remember my cat's name?", "What did you think of the picture?",
    "Did you like the recipe I sent?", "Remember that budget I showed you?",
    "What was it about again?", "Was the photo cute?",
]

# What the user ASKS FOR, in any language (multi-language, 2026-11-19): the
# English/Japanese word rules in chat.py stay first; these catch the other
# languages ("tolong cari", "帮我查一下", "ค้นหาให้หน่อย").
SEARCH_ASK = [
    "Can you search that for me?", "Look it up online, please.", "Google it.",
    "Search the web for the new iPhone price.", "Find out online when the store opens.",
    "Check the internet for the latest news about the election.",
    "Can you look up the movie's release date?", "Search for reviews of that restaurant.",
    "Do a quick web search on it.", "Look up who won the match last night.",
    "Can you check online whether the trains are running?",
]
TIME_ASK = [
    "What time is it now?", "What's today's date?", "What day is it today?",
    "What's the time right now?", "Tell me the current time.", "What's the date today?",
    "Which day of the week is it?",
]
LINK_ASK = [
    "Send me the link.", "Can you give me the URL?", "What's the website address?",
    "Where's the source link?", "Share the link to that page.", "Give me the links to those articles.",
]
AGAIN_ASK = [
    "Try again.", "Search it again.", "Look it up again, please.", "One more time.",
    "Check again, maybe it changed.", "Refresh that search.", "Can you retry?",
    "I turned the web back on, try now.", "Please try once more.",
]
NOT_ASK = [
    "I searched everywhere for my keys.", "I googled it yesterday but found nothing.",
    "I don't have time today.", "Time flies so fast.", "We had a great time.",
    "The website I made is finally online.", "I lost the link to my old blog years ago.",
    "Again, I'm sorry about yesterday.", "I tried again and failed again.",
    "My friend is always online.", "I'll look for a new job next year.",
    "What a day it has been.", "Let's meet again next week.",
]

SHARED_FILE = {
    "picture": ["What do you think of the picture I sent you?",
                "Did you see the photo I showed you?", "Look at that pic I sent again.",
                "The photo I sent earlier, what's in it?",
                "Can you check the screenshot I sent?",
                # asking about something IN a file (2026-11-15: "the ramen recipe,
                # how long do i boil the eggs?" was not file talk; subjects kept
                # unlike the test set's on purpose)
                "Was the dog photo blurry?", "Is that selfie of mine still cute to you?",
                "What was written on the sign in that pic?", "Does my sleep chart look okay?",
                "What time did my schedule screenshot say for Friday?"],
    "video": ["What did you think of the video I sent?", "Did you watch the clip I shared?",
              "About that video I showed you earlier...", "Did you like the vid I sent?",
              "Watch the video I sent you again.",
              "In that clip, what song was playing?",
              "The concert video, could you hear the singer?"],
    "document": ["Did you read the file I sent?", "What did you think of my document?",
                 "Look at the PDF I gave you again.", "Check the report I attached.",
                 "The document I shared earlier, can you summarize it?",
                 "In the pasta recipe, how much salt do I add?",
                 "What time is the flight in my itinerary again?",
                 "What was the total in my expenses sheet?",
                 "That essay of mine, is the ending too long?"],
}
OTHER_MEDIA = [
    "I watched a funny video on YouTube.", "My first video game was Pokemon.",
    "The last movie I saw was great.", "I took a lot of photos on my trip.",
    "I'll send you the pictures later.", "I deleted some videos from my phone.",
    "I need to file my taxes.", "I have to write a report for work.",
    "My photo shoot went badly.", "Saw a video of a cat falling off a sofa.",
    "I'm editing a video for my channel.", "I printed some documents at work.",
    "Do you like taking selfies?", "I'm watching a movie right now.",
    "There was a documentary about space on TV.",
]

# =============================================================================

_lock = threading.Lock()
_examples = {}          # text -> vector (filled once by _warm)
_ready = False
_started = False
_last_try = 0.0
_recent_vecs = {}       # text -> vector: one reply checks the same texts several times
RECENT_KEEP = 32
_embed_fn = None        # tests / measuring inject a local embedder here


def _all_examples() -> list:
    out = (WEATHER_ASK + CHAT + FORECAST + NOT_FORECAST + OTHER_MEDIA
           + LOOKUP_CLAIM + NOT_LOOKUP_CLAIM + LOOK_AGAIN + NOT_LOOK_AGAIN
           + SEARCH_ASK + TIME_ASK + LINK_ASK + AGAIN_ASK + NOT_ASK)
    for group in SHARED_FILE.values():
        out += group
    return list(dict.fromkeys(out))


def _http_embed(texts, timeout):
    req = urllib.request.Request(
        SIDECAR_URL + "/embed", data=json.dumps({"texts": list(texts)}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        return None
    return data.get("vectors")


def _embed(texts, timeout):
    """Vectors for texts, or None (any failure)."""
    try:
        if _embed_fn is not None:
            return [list(v) for v in _embed_fn(list(texts))]
        return _http_embed(texts, timeout)
    except Exception:
        return None


def _warm() -> bool:
    """Fetch the example vectors once (64 per call)."""
    global _ready, _last_try
    _last_try = time.monotonic()
    texts = _all_examples()
    got = {}
    for i in range(0, len(texts), 64):
        chunk = texts[i:i + 64]
        vecs = _embed(chunk, WARM_TIMEOUT)
        if not vecs or len(vecs) != len(chunk):
            return False
        got.update(zip(chunk, vecs))
    with _lock:
        _examples.clear()
        _examples.update(got)
        _ready = True
    print(f"[Amadeus] Meaning checks ready ({len(got)} examples).")
    return True


def start() -> None:
    """Fetch the example vectors in the background (main.py, at launch)."""
    global _started
    if not ENABLED or _started:
        return
    _started = True

    def loop():
        while not _warm():
            time.sleep(WARM_RETRY_SECONDS)
    threading.Thread(target=loop, daemon=True, name="meaning-warm").start()


def use_embedder(fn) -> None:
    """Tests / measuring: embed with fn(list of texts) -> vectors, now."""
    global _embed_fn, _started
    _embed_fn, _started = fn, True
    _recent_vecs.clear()
    _desc_cache.clear()
    _warm()


def reset() -> None:
    global _embed_fn, _started, _ready
    _embed_fn, _started, _ready = None, False, False
    _examples.clear()
    _recent_vecs.clear()
    _desc_cache.clear()


def available() -> bool:
    return ENABLED and _ready


def _vec(text: str):
    text = (text or "").strip()
    if not text or not available():
        return None
    v = _recent_vecs.get(text)
    if v is not None:
        return v
    vecs = _embed([text], TIMEOUT)
    if not vecs:
        return None
    _recent_vecs[text] = vecs[0]
    while len(_recent_vecs) > RECENT_KEEP:
        _recent_vecs.pop(next(iter(_recent_vecs)))
    return vecs[0]


def _dot(a, b) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def _best(v, group) -> float:
    return max((_dot(v, _examples[t]) for t in group if t in _examples), default=0.0)


def scores(text: str, yes, no):
    """(best match in yes, best match in no) or None."""
    v = _vec(text)
    if v is None:
        return None
    return _best(v, yes), _best(v, no)


def _decide(text, yes, no, least, margin):
    s = scores(text, yes, no)
    if s is None:
        return None
    return s[0] >= least and s[0] - s[1] >= margin


def is_weather_question(text: str):
    """True / False, or None (unknown - use the word rules)."""
    return _decide(text, WEATHER_ASK, CHAT, WEATHER_MIN, WEATHER_MARGIN)


def states_forecast(*texts):
    """Does her reply state live weather (a forecast, a chance of rain, a
    temperature now)? The first text that can be judged decides (her own
    words first, the translation only when they are empty) - one check,
    not two."""
    for t in texts:
        if (t or "").strip():
            answer = _decide(t, FORECAST, NOT_FORECAST, FORECAST_MIN, FORECAST_MARGIN)
            if answer is not None:
                return answer
    return None


def claims_lookup(*texts):
    """Does her reply say she searched / looked something up / checked
    online? The first text that can be judged decides (her own words first),
    like states_forecast. None when unknown."""
    for t in texts:
        if (t or "").strip():
            answer = _decide(t, LOOKUP_CLAIM, NOT_LOOKUP_CLAIM, CLAIM_MIN, CLAIM_MARGIN)
            if answer is not None:
                return answer
    return None


def shared_file_kind(text: str):
    """"picture" / "video" / "document" when the message is about a file the
    user sent in this chat, "" when it is not, None when unknown (or ADD is
    switched off)."""
    if not RECALL_ADD:
        return None
    v = _vec(text)
    if v is None:
        return None
    other = _best(v, OTHER_MEDIA)
    kind, best = max(((k, _best(v, g)) for k, g in SHARED_FILE.items()), key=lambda x: x[1])
    if best >= FILE_MIN and best - other >= FILE_MARGIN:
        return kind
    return ""


def other_media(text: str):
    """True when the message is clearly about some OTHER video / photo /
    file (a game, a movie, photos on their phone), not one sent in this
    chat - a word-rule recall is then dropped. None when unknown."""
    if not RECALL_BLOCK:
        return None
    s = scores(text, sum(SHARED_FILE.values(), []), OTHER_MEDIA)
    if s is None:
        return None
    return s[0] - s[1] <= BLOCK_BELOW


_desc_cache = {}
DESC_KEEP = 400            # file descriptions remembered (they never change)
BATCH_TIMEOUT = 4.0        # describing many files at once (the first time only)


def fits(text: str, descriptions):
    """[how close the message is to each description (0..1)], or None.
    New descriptions go to the sidecar in one batch; known ones are kept."""
    v = _vec(text)
    if v is None:
        return None
    descs = [(d or "").strip() for d in descriptions]
    missing = list(dict.fromkeys(d for d in descs if d and d not in _desc_cache))
    for i in range(0, len(missing), 64):
        chunk = missing[i:i + 64]
        got = _embed(chunk, BATCH_TIMEOUT)
        if not got or len(got) != len(chunk):
            return None
        _desc_cache.update(zip(chunk, got))
    while len(_desc_cache) > DESC_KEEP:
        _desc_cache.pop(next(iter(_desc_cache)))
    return [(_dot(v, _desc_cache[d]) if d in _desc_cache else 0.0) for d in descs]


def asks_to_look_again(text: str):
    """The user wants her to really look at / read a file again (not just
    remember it). None when unknown."""
    return _decide(text, LOOK_AGAIN, NOT_LOOK_AGAIN, LOOK_MIN, LOOK_MARGIN)


def fit(text: str, description: str):
    """How close a message is to a file's description (0..1), or None."""
    v = _vec(text)
    description = (description or "").strip()
    if v is None or not description:
        return None
    d = _desc_cache.get(description)
    if d is None:
        got = _embed([description], TIMEOUT)
        if not got:
            return None
        d = _desc_cache[description] = got[0]
        if len(_desc_cache) > 200:
            _desc_cache.pop(next(iter(_desc_cache)))
    return _dot(v, d)


def misfit(text: str, description: str) -> bool:
    """A word-rule recall whose file does not fit the message in meaning."""
    if not RECALL_BLOCK:
        return False
    f = fit(text, description)
    return f is not None and f < FIT_MIN


def _asks(text, group, least, margin):
    others = CHAT + NOT_ASK
    return _decide(text, group, others, least, margin)


def asks_to_search(text: str):
    """The user asks her to search the web / look something up online.
    None when unknown."""
    return _asks(text, SEARCH_ASK, ASK_SEARCH_MIN, ASK_MARGIN)


def asks_time(text: str):
    """The user (or a search query) asks for the current time or date."""
    return _asks(text, TIME_ASK, ASK_TIME_MIN, ASK_TIME_MARGIN)


def asks_for_link(text: str):
    """The user asks for a link / web address / source."""
    return _asks(text, LINK_ASK, ASK_LINK_MIN, ASK_MARGIN)


def asks_again(text: str):
    """The user asks her to try / search / check again."""
    return _asks(text, AGAIN_ASK, ASK_AGAIN_MIN, ASK_MARGIN)

