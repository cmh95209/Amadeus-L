# Amadeus architecture map

A one-page map of the code for anyone changing it: contributors, testers
reading logs, and AI coding agents. It says **where things are**; the code and
its comments say how they work. User-facing docs live in [`docs/`](docs/).

**Keep it current.** A change that adds, removes, renames or moves the job of a
file updates this map in the same commit.
`backend/tests/test_architecture_map.py` fails when a Python file in
`backend/`, `memory_sidecar/` or `scripts/` is not listed here.

---

## 1. The running parts

| Part | Address | Started by | Code |
|---|---|---|---|
| Page (React + TypeScript, Live2D) | `127.0.0.1:5173` | launcher (built copy via `vite preview`; Vite dev server as fallback) | `frontend/` |
| Backend (Flask) | `127.0.0.1:5050` | launcher | `backend/main.py` -> `backend/api.py` |
| Voice engine (GPT-SoVITS) | `127.0.0.1:9880` | launcher | `backend/start_gptsovits.py`; the engine is cloned into `GPT-SoVITS/` (not in git) |
| Long-term memory sidecar (CharacterMemory) | `127.0.0.1:9870` | launcher (own Python venv) | `memory_sidecar/` |
| Built-in local model (llama-server, optional) | `127.0.0.1:8091` | the backend (`builtin_server.py`) | - |
| The user's own model server or cloud provider | any | the user, outside the app | - |

The launcher (`scripts/launcher.py`) starts the parts, waits for each, writes
one log per part to `logs/`, and stops everything on exit. The sidecar is
**soft-fail**: when it is down, chat works exactly as without memory.

## 2. One message, end to end

```text
page  POST /  (frontend/src/api.ts -> backend/api.py request_message)
  web access OFF (default): chat.getOutputPackedVoiceFirst()
    1. store the user turn (memory.py), mirror it to the sidecar (cm_bridge.save_turn)
    2. build the prompt (section 3) and make ONE model call -> her line in her
       voice language (AmadeusReply format, guarded and retried in chat.py)
    3. stream event "voice": speech id -> page opens GET /speech/<id>
       -> tts.streamVoiceChunks -> GPT-SoVITS :9880 -> audio streams to the page
    4. background: one short call writes the translation line in the user's
       display language (chat._backfill_english / _translate_reply_en)
       -> stream event "text"
    5. after the turn: sidecar mirror of her line, background trust check
       (run_trust_rescore; it also updates concerns), rolling-summary fold
  web access ON: chat.getOutputPacked() inside api._web_stream
    triage (weather / explicit search / neither) -> tool loop with web_search
    and get_weather -> pages read (webreader.py) -> one AmadeusPack answer
```

Other entry points in `chat.py`: `generate_greeting` (startup, tab switch, wake;
greetings READ the sidecar but never WRITE to it), `regenerateReply`,
`regenerate_greeting`, the picture reaction, and special interactions
(`chat_interactions.py`). The first-run ceremony is `ceremony.py`.

## 3. Her prompt

`chat._ordered_prompt` builds every prompt. Things that stay the same go first,
so providers can reuse (and discount) the start of the prompt:

1. **Fixed part**, merged into ONE system message
   (`chat._merge_leading_system_messages`; some local chat templates accept only
   one): personality -> voice rules (`ja_voice.py`) -> reply-format rules -> web
   rules (on/off) -> other-tabs catch-up (`cross_tab.py`) -> this tab's running
   summary.
2. **Chat history**: this tab's recent turns word for word
   (`memory.build_prompt_messages`, inside the token budget).
3. **Changing part**: closeness, feelings, concerns, character book (when asked),
   long-term memory (`cm_bridge.fetch_memory_block`), search results from old
   chats, lookup and picture index, time and situation.
   - Built-in model or any cloud provider: placed after the newest user message
     as one "RIGHT NOW" note (`chat._right_now_note`; sent as a **user** message
     to cloud providers by `llm._right_now_note_as_user`).
   - The user's own local or LAN server: inside the fixed system message.

Pictures are added after building (`attachments.with_pictures`). Stored history uses
text labels. With consent, ChatGPT attachment reactions can also use media previews.

## 4. Where settings and user data live

| What | Where | Notes |
|---|---|---|
| Model server address, model name | `backend/llm_server.txt`, `backend/data/llm_model.txt` | tracked with defaults, then personal |
| Personality | `backend/data/personality.txt` (default: `personality.default.txt`) | user-editable; `defaults_refresh.py` updates untouched old defaults |
| API keys, Vertex sign-in, chosen provider | `backend/data/api_key.txt`, `api_keys.json`, `vertex_key.json`, `llm_provider.txt` | **secret, git-ignored** |
| Built-in model choice | `backend/data/local_llm.json` | written by the installer |
| Display language, voice language | `backend/language.txt`, `backend/voice_language.txt` | read fresh each time |
| Other switches (web access, sampling, cross-tab, greeting cooldown, ...) | small files in `backend/data/` | read live by the running app; `memory.py` holds most readers |
| **Chats, tabs, attachments, voice files** | `backend/data/memory.db`, `backend/data/attachments/` | user data |
| **Trust and other stats** | `backend/data/stats.db` (`stats.py`) | user data |
| **Long-term memory** | `backend/data/character_memory/` (owned by the sidecar) | user data |
| Launcher state, page mode | `.runtime/` | internal |

Personal files that git tracks with a default show as "modified" on a
user's PC: that is normal. Never commit them, and never put keys, chats or
memory data in an issue, log paste or test fixture.

## 5. Backend modules (`backend/`)

| File | Owns |
|---|---|
| `main.py` | process entry: patches, UTF-8 console, default refresh, starts the Flask app |
| `api.py` | every HTTP route the page calls (chat, settings, attachments, audio, system) |
| `chat.py` | **the core**: reply and greeting pipelines, web-search loop, triage, honesty nets and retries, prompt ordering, translation line, trust re-check, summary merge |
| `memory.py` | SQLite store (`memory.db`): messages, tabs, versions, presence, attachments index; most setting readers; `build_prompt_messages`; rolling summary |
| `llm.py` | model client (`get_llm`), server address probing, local-vs-cloud rules, reasoning levels |
| `context_window.py` | provider limits and background metadata caching, prompt token accounting and history selection |
| `chatgpt_auth.py` | ChatGPT sign-in, renewal, sign-out and consent |
| `chatgpt_llm.py` | ChatGPT Responses client, reasoning levels and background usage limits |
| `origin_guard.py` | browser origin, host and route checks |
| `providers.py` | the provider list in Settings and per-provider keys |
| `vertex_auth.py` | Google Vertex AI sign-in (service-account JSON -> short-lived token) |
| `vertex_priority.py` | the Vertex "Priority" switch (request headers) |
| `cloud_retry.py` | waits and retries when a cloud provider answers busy (429/503) |
| `reply_errors.py` | plain error messages when a reply cannot be completed |
| `builtin_llm.py` | talking to the built-in model (mode, file choice, fixed alias) |
| `builtin_server.py` | starting/stopping the built-in llama-server on :8091 |
| `cm_bridge.py` | backend calls to the memory sidecar: `save_turn`, `learn_now`, `fetch_memory_block` |
| `memory_plan_bridge.py` | authenticated loopback route for consented sidecar extraction on the ChatGPT plan |
| `meaning.py` | meaning checks through the sidecar's embedder (`/embed`); returns None when it is off, callers keep their old rule |
| `cross_tab.py` | what she knows about the user's other tabs (catch-up block, search block) |
| `moment_recall.py` | scene boundaries, recall evidence, cadence and excerpt rendering |
| `transcript_vector_index.py` | disposable transcript embeddings and the background index worker |
| `cross_files.py` | finding a file the user sent in another chat |
| `attachments.py` | pictures and documents: upload, shrink, captions, her desk, `with_pictures` |
| `video.py` | videos and animated GIF/WebP: resumable upload, watch copy, frames, preparation worker |
| `gifs.py` | online GIF and sticker search through KLIPY and GIPHY, media downloads |
| `picker.py` | picker state and selected media staged as attachment cards |
| `picker_store.py` | recents, favourites and install ID in `backend/data/picker.json`, offline media copies |
| `stickers.py` | imports, searches and manages user sticker packs in `backend/data/stickers/` |
| `transcribe.py` | speech in videos -> text with times (runs `transcribe_helper.py`) |
| `transcribe_helper.py` | the faster-whisper step, run with the voice engine's Python |
| `ceremony.py` | the first-launch ceremony and its saved state |
| `greeting_cooldown.py` | no second greeting right after a reopen |
| `chat_interactions.py` | touch/pat/tap events and her recorded answers |
| `ja_voice.py` | her editable voice rules and the closeness-tier context |
| `stats.py` | key-value stats (trust and others) in `stats.db` |
| `feelings.py` | short-term feelings that fade by half-life |
| `concerns.py` | things on her mind about the user, with dates |
| `timing.py` | "[Timing]" log lines per reply stage (measure only) |
| `source_tags.py` | labels saying where each prompt item came from |
| `live_lookup.py` | her `get_weather` tool and the "nothing looked up yet" note |
| `weather.py` | live weather and air quality from Open-Meteo |
| `websearch.py` | keyless search engines and their fallback order |
| `webfetch.py` | reading a URL into clean text (bounded, private-address guard) |
| `webreader.py` | reader mode / quick mode: notes from pages for her answer |
| `webpdf.py` | bounded text extraction from PDF search results |
| `weblinks.py` | links the user pastes: find and open them |
| `webdates.py` | dates in web text relative to today |
| `tts.py` | voice client for GPT-SoVITS: streaming to the page, saved files |
| `speech_text.py` | rewriting text into speakable form (numbers, names, English words) |
| `voice_gate.py` | lets background model work wait while her voice is made |
| `voice_lang.py` | the "Voice language" setting |
| `display_lang.py` | the "Your language" setting |
| `start_gptsovits.py` | starts the voice engine with Windows audio fixes |
| `restart_control.py` | restart from Settings and the parts' live status (via the launcher) |
| `remote_access.py` | phone access through Tailscale (`docs/remote-access.md`) |
| `defaults_refresh.py` | updates untouched old default files after an update |
| `console_encoding.py` | forces a UTF-8 console |
| `patches.py` | small compatibility patches applied at startup |

## 6. Page (`frontend/src/`)

| File | Owns |
|---|---|
| `App.tsx` | the whole screen: chat, tabs, Settings, status (the biggest file) |
| `api.ts` | shared backend address (`API_BASE`), chat and settings calls, reply stream |
| `audio/SpeechPlayer.ts`, `audio/voicePacing.ts` | playing her voice smoothly |
| `greetingFlow.ts` | placing greetings and the status bar text (`deriveFooter`) |
| `attachments.ts`, `useAttachments.ts` | pictures, documents, videos in the composer |
| `sources.ts`, `components/SourcesBar.tsx` | the "where this came from" chips |
| `providers.ts`, `components/ModelPicker.tsx` | Settings: model provider and model |
| `components/ChatGPTConnection.tsx`, `components/ChatGPTComposerPicker.tsx` | ChatGPT sign-in, consent, model and reasoning controls |
| `picker/` | emoji, GIF and sticker picker, preferences and inline chat media |
| `scripts/build_emoji_data.mjs` (under `frontend/`) | builds the picker's Unicode emoji data and localized search names |
| `i18n.ts`, `lang/*.json` | page text in every display language |
| `live2d/`, `components/Live2DCharacter.tsx` | her Live2D model |
| `components/` | the other panels (mood, system, mobile access, quit, ...) |

## 7. Memory sidecar (`memory_sidecar/`)

| File | Owns |
|---|---|
| `memory_sidecar/cm_sidecar.py` | the service on :9870: `/health`, `/save`, `/context`, `/extract` (learning), `/embed` (Qwen3-Embedding-0.6B), `/memories` |
| `memory_sidecar/transcript_embed_jobs.py` | the single background job slot for transcript embeddings |

`lib/` is the vendored CharacterMemory engine (GPL-3.0, kept in its own
process). `seed_index/` is her lore, seeded on first start. A durable SQLite
queue runs learning through the configured provider or through the app's
ChatGPT bridge when the user has signed in and enabled background use.

## 8. Launcher and scripts (`scripts/`)

| File | Owns |
|---|---|
| `scripts/launcher.py` | starts, watches and stops every part; builds the page; sidecar venv |
| `scripts/install_windows.ps1`, `install_linux.sh`, `install_macos.sh` | one-shot installers (follow `main`) |
| `scripts/make_pinned_installers.py` | each release's pinned installers |
| `scripts/llm_tiers.py` | built-in model tiers by graphics memory (`local_llm_tiers.json`) |
| `scripts/lang_her.py` | collects her fixed lines and error texts into `frontend/src/lang/en.json` |
| `scripts/lang_fill.py` | translates the lines a language file is missing (`--check` lists them) |
| `scripts/measure_asks.py`, `scripts/measure_meaning.py` | measuring tools for `meaning.py` against the running sidecar (development only) |
| `scripts/bench_embed_order.py` | measures embedding latency for web search result ordering |

New page text: write the English, run `node frontend/scripts/lang_extract.mjs`,
then `scripts/lang_her.py` and `scripts/lang_fill.py`.

Root start files: `start_windows.bat`, `start_linux.sh`, `start_macos.command`
(plus "no model" variants), `Update Amadeus.bat`, `check_amadeus.bat`.

## 9. Want to change X? Look here

| Change | Start in |
|---|---|
| what she says / reply format / a guard or retry | `chat.py` (search the section banners) |
| what goes into her prompt, and in what order | `chat._ordered_prompt`, `memory.build_prompt_messages` |
| context window, history budgets and recall | `context_window.py`, `memory.py`, `moment_recall.py`, `transcript_vector_index.py` |
| her personality or speaking rules | `backend/data/personality.default.txt`, `ja_voice.py` |
| a new setting | reader in `memory.py` (or its own module), route in `api.py`, UI in `App.tsx` + `api.ts`, text in `lang/*.json` |
| a new model provider | `providers.py`, `llm.py` |
| ChatGPT sign-in, consent or model selection | `chatgpt_auth.py`, `chatgpt_llm.py`, `origin_guard.py`, `components/ChatGPTConnection.tsx`, `components/ChatGPTComposerPicker.tsx` |
| long-term memory | `cm_bridge.py` (app side), `memory_sidecar/cm_sidecar.py` |
| greetings | `chat.generate_greeting`, `memory.compute_greeting_situation`, `frontend/src/greetingFlow.ts` |
| voice | `tts.py`, `speech_text.py`, `voice_lang.py`, `audio/SpeechPlayer.ts` |
| web search / weather | `chat.py` web sections, `websearch.py`, `webreader.py`, `weather.py` |
| reading web PDFs | `webfetch.py`, `webpdf.py`, `webreader.py` |
| attachments / videos | `attachments.py`, `video.py`, `frontend/src/attachments.ts` |
| emoji, GIF and sticker picker | `frontend/src/picker/`, `picker.py`, `gifs.py`, `picker_store.py`, `stickers.py` |
| start-up, ports, logs | `scripts/launcher.py` |

## 10. Traps that break changes

- **One system message.** The fixed part must stay one leading system message.
  New fixed text goes through `_ordered_prompt`, not as an extra message.
- **Pass the tab.** Store helpers default to the ACTIVE tab. Any background or
  greeting path must pass its `conversation_id` explicitly.
- **Greetings never write to the sidecar** (`save_turn` stays out of the greeting
  path; `tests/test_cm_bridge.py` checks it).
- **Memory is optional.** `cm_bridge` and `meaning.py` must degrade to "no memory"
  or "old rule" when the sidecar is down, slow or still loading, and never raise.
- **Prompt cache.** Anything that changes every turn belongs in the changing
  part, or it makes every prompt new for the provider.
- **Label sync.** `llm.RIGHT_NOW_LABEL` must equal `chat._RIGHT_NOW_LABEL`
  (`tests/test_prompt_order.py`).
- **No per-language word lists** for new behaviour: use the model or `meaning.py`.
- **Line endings.** Committed files are LF.

## 11. Tests

- Backend (from `backend/`, with the app's Python environment):
  `python -m pytest tests -n 4 --dist worksteal -q`
  (or `python -m unittest discover -s tests -p "test_*.py"`). One file:
  `python -m pytest tests/test_x.py -q`.
- Page (from `frontend/`): `node tests/run.mjs` and
  `node node_modules/typescript/bin/tsc -b`.
