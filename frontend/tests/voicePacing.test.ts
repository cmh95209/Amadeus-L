import assert from "node:assert/strict";
import {
  decide,
  lineLengthGuess,
  neededLead,
  loadVoicePlayback,
  pacingKeys,
  voiceLangFromHeader,
  voiceStartsEarly,
  SECONDS_PER_CHAR_START,
  type PacingInput,
} from "../src/audio/voicePacing";

const base: PacingInput = {
  mode: "smooth", received: 1, ahead: 1, elapsed: 1, total: 10, ended: false, priorRate: null,
};

// A fast PC (more than real time): she starts at once, as before.
assert.equal(decide({ ...base, received: 0.3, ahead: 0.3, elapsed: 0.1, priorRate: 2 }).go, true);
assert.equal(decide({ ...base, received: 2, ahead: 2, elapsed: 1 }).go, true);

// Nothing learned yet: watch the engine for a moment first.
assert.equal(decide({ ...base, received: 0.2, ahead: 0.2, elapsed: 0.2 }).go, false);

// Half real time, 10 s line: start once making the rest finishes before
// playing it would - (10 - R) / 0.5 + 0.4 <= R  ->  R >= 5.2 s.
const slowAt = (r: number, total = 10) =>
  ({ ...base, total, received: r, ahead: r, elapsed: r / 0.5 });
assert.equal(decide(slowAt(5)).go, false);
assert.equal(decide(slowAt(5.5)).go, true);
const halfway = decide(slowAt(3.5));
assert.ok(halfway.progress > 0.4 && halfway.progress < 0.6, `progress ${halfway.progress}`);
// The same measured from the first moments, with the speed learned before.
assert.equal(decide({ ...slowAt(0.4), priorRate: 0.5 }).go, false);

// A guess that was too short never holds her back for long: the line is
// taken to be at least what already arrived.
assert.equal(decide(slowAt(3, 2)).go, true);

// Right away never waits; When fully ready waits for the end.
assert.equal(decide({ ...base, mode: "instant", received: 0.1, ahead: 0.1, elapsed: 0 }).go, true);
assert.equal(decide({ ...base, mode: "whole", priorRate: 5, received: 9, ahead: 9 }).go, false);
assert.equal(decide({ ...base, mode: "whole", ended: true }).go, true);
assert.equal(decide({ ...base, received: 0, ahead: 0, ended: true }).go, false, "an empty line has nothing to play");

// The saved choice, and the old "Start her voice early" switch moving over.
const store = (items: Record<string, string>) => ({ getItem: (k: string) => items[k] ?? null });
assert.equal(loadVoicePlayback(store({})), "smooth");
assert.equal(loadVoicePlayback(store({ amadeusVoiceEarly: "1" })), "early");
assert.equal(loadVoicePlayback(store({ amadeusVoiceEarly: "0" })), "smooth");
assert.equal(loadVoicePlayback(store({ amadeusVoicePlayback: "whole", amadeusVoiceEarly: "1" })), "whole");
assert.equal(loadVoicePlayback(store({ amadeusVoicePlayback: "nonsense" })), "smooth");
assert.equal(loadVoicePlayback({ getItem() { throw new Error("blocked"); } }), "smooth");
assert.equal(loadVoicePlayback(null), "smooth");
assert.deepEqual(["smooth", "early", "instant", "whole"].map(m => voiceStartsEarly(m as never)),
  [false, true, true, false]);

// Sentence by sentence: a fast first sentence, then a 3 s pause before the
// next. The head start that avoids every gap = the worst "arrived later than
// it would have been played" moment.
assert.equal(neededLead([[0, 2], [0.5, 4], [7, 6], [7.5, 8]]), 3);
assert.equal(neededLead([[0, 1], [0.3, 2], [0.6, 3]]), 0, "always ahead: no head start");
assert.equal(neededLead([]), 0);
// A learned head start holds a fast-looking start back until it has passed.
const quick = { ...base, priorRate: 2, received: 2, ahead: 2, elapsed: 1, lead: 3.4 };
assert.equal(decide(quick).go, false, "fast start, but sentence breaks need 3.4 s");
assert.ok(decide(quick).progress > 0.2 && decide(quick).progress < 0.4);
assert.equal(decide({ ...quick, elapsed: 3.5, received: 7, ahead: 7 }).go, true);
assert.equal(decide({ ...quick, mode: "instant" }).go, true, "Right away ignores it");
assert.equal(decide({ ...quick, lead: null }).go, true);

// -- per voice language (the /speech header X-Amadeus-Voice-Lang) ------------
// The header names the line's voice language; none (old backend) = Japanese.
assert.equal(voiceLangFromHeader(null), "ja", "a missing header is Japanese");
assert.equal(voiceLangFromHeader("ja"), "ja");
assert.equal(voiceLangFromHeader("en"), "en");
assert.equal(voiceLangFromHeader("zh"), "zh");
assert.equal(voiceLangFromHeader("yue"), "yue");
assert.equal(voiceLangFromHeader("ko"), "ko");
assert.equal(voiceLangFromHeader("xx"), "ja", "an unknown code counts as Japanese");

// Japanese keeps the ORIGINAL localStorage keys (what a browser learned
// before the setting exists keeps working); the others get a ".<code>" suffix.
assert.deepEqual(pacingKeys("ja"), {
  rate: "amadeusVoiceRate",
  secPerChar: "amadeusVoiceSecPerChar",
  lead: "amadeusVoiceLead",
}, "ja: the original keys, unchanged");
assert.deepEqual(pacingKeys("en"), {
  rate: "amadeusVoiceRate.en",
  secPerChar: "amadeusVoiceSecPerChar.en",
  lead: "amadeusVoiceLead.en",
});
assert.deepEqual(pacingKeys("zh"), {
  rate: "amadeusVoiceRate.zh",
  secPerChar: "amadeusVoiceSecPerChar.zh",
  lead: "amadeusVoiceLead.zh",
});
assert.deepEqual(pacingKeys("yue"), {
  rate: "amadeusVoiceRate.yue",
  secPerChar: "amadeusVoiceSecPerChar.yue",
  lead: "amadeusVoiceLead.yue",
});
assert.deepEqual(pacingKeys("ko"), {
  rate: "amadeusVoiceRate.ko",
  secPerChar: "amadeusVoiceSecPerChar.ko",
  lead: "amadeusVoiceLead.ko",
});

// A fresh browser (nothing learned) guesses a line's length from the
// language's START value (deliberately on the slow side).
assert.equal(lineLengthGuess("ja", 100, null), 16, "ja: today's default 0.16 s/char");
assert.equal(lineLengthGuess("ja", 100, null), 100 * SECONDS_PER_CHAR_START.ja);
assert.equal(lineLengthGuess("en", 100, null), 7.5);
assert.equal(lineLengthGuess("zh", 100, null), 22);
assert.equal(lineLengthGuess("yue", 100, null), 22);
assert.ok(Math.abs(lineLengthGuess("ko", 100, null) - 14) < 1e-9);
// ...but what the browser learned for that language always wins.
assert.equal(lineLengthGuess("en", 100, 0.1), 10);
assert.equal(lineLengthGuess("ja", 100, 0.2), 20);

console.log("PASS: voice pacing - fast PCs start at once, slow PCs wait just enough, sentence-break head start, saved choice + old switch, per-voice-language keys + start values");
