# LiveSubtitle inference engine

Run from the application root:

```powershell
.\runtime\python\python.exe engine\server.py --root . --port 17865
```

The API binds only to `127.0.0.1`. Its persistent random bearer token is created in
`config/engine-token.txt`. Only `GET /health` is public. Native clients omit Origin;
browser clients must use a `chrome-extension://<extension-id>` origin and the token.
Model import additionally requires a native client without an Origin header.

Without saved settings, the engine selects local Qwen3-ASR-1.7B
(`models/asr/qwen3-asr-1.7b`) and HY-MT2-7B Q6_K
(`models/translation/HY-MT2-7B-Q6_K.gguf`), automatic input-language detection,
Korean output, the default (`legacy`) ASR profile, empty hints, and sentence
boundary recheck off. Saved user selections take precedence; changing these
defaults does not rewrite existing settings. The Windows UI initially uses
browser placement for the overlay and preserves its saved placement afterward.

Modules:

- `server.py`: HTTP/WS API, authentication, lifecycle, CLI.
- `http_guard.py`: authentication and 64 KiB HTTP body cap before JSON parsing.
- `settings.py`: validated settings, current-user DPAPI key storage.
- `models.py`: local inventory and exclusive-copy GGUF import.
- `runtime.py`: owned hidden llama-server process and lazy local ASR backends.
- `asr_qwen.py`: local-only Qwen3-ASR Transformers inference and whole-waveform speech check.
- `translation_profiles.py`: MiLMMT/Hy-MT2/TranslateGemma detection, source-language resolution, and dedicated prompts.
- `glossary.py`: bounded read-only user glossary loading and exact current-source term matching.
- `audio.py`: byte-bounded PCM queue, utterance boundaries, overlap removal.
- `fast_qwen.py` / `fast_qwen_session.py`: default Qwen endpoint hints, speculative ASR cache, and final-only translations.
- `asr_warmup.py`: bounded ASR-only preparation on disposable synthetic audio.
- `text_normalization.py`: comparison-only Chinese script equivalence; displayed text stays unchanged.
- `sessions.py`: one active audio session, adapters, cancellation, caption pipeline.
- `streaming_sentences.py`: stable interim sentences and authoritative final reconciliation.
- `local_captions.py`: bounded early Korean caption revisions over local ASR results.
- `stable_sessions.py`: concurrent PCM ingestion and final-only local subtitles.
- `qwen_streaming.py`: retained-PCM CJK agreement and same-caption corrections.
- `speech_gate.py`: optional Silero endpoint hints; never an authoritative Qwen input filter.
- `asr_alignatt.py`: local CT2 encoder / Torch attention decoder, with pinned source and licenses in `_vendor`.

`asr_profile` selects `legacy` (default, 기본), `stable` (반복확인(느림)),
or `alignatt`. Explicit saved selections are preserved. Default Qwen uses the
fast endpoint path described below; its four-second maximum remains unchanged.
The stable path keeps receiving original PCM while one ASR request runs. At a
200 ms pause it can precompute recognition and reuse it after a 500 ms endpoint
if no speech resumed. Only committed text is translated/displayed in Korean.
Qwen retains the current utterance's complete waveform, checks expanding CJK
hypotheses, and corrects existing sentence IDs instead of adding duplicate
captions. Silero helps endpoint detection only after speech is detected inside
that window; negative predictions cannot suppress RMS-positive input. A 12 s
maximum window bounds continuous speech, so a forced boundary can still divide
a word. Stable Whisper retains its timed-word agreement and avoids provisional
Korean translations. Legacy Whisper retains its provisional Korean captions;
default Qwen's early ASR cache is not a provisional-caption path.

Whisper AlignAtt is an experimental alternative. It uses genuinely committed
attention-decoder tokens and groups them by punctuation, estimated word pauses,
or approximately three seconds of committed speech. It needs matching local
`alignatt-decoder.pt` or `large-v3-turbo.pt` inside the selected CT2 model folder.
The downloaded official turbo checkpoint's SHA-256 is
`aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a`.
LayerNorm stays FP32 while the decoder uses FP16. Windows uses the upstream
Torch median fallback when Triton is unavailable. Models remain local and are
excluded from backups; separate inference formats may coexist in `models`.
AlignAtt and stable profiles do not guarantee lower latency or better accuracy
for every recording. See the [project README](../README.md) for published test results.

The engine never downloads models. `local` mode performs recognition and translation
locally. `gemini` mode sends audio to the documented Gemini transcription WebSocket;
translation remains local. API keys stay in memory unless `save_gemini_key: true` is
explicitly supplied together with `gemini_api_key`. That opt-in persists only a
Windows CurrentUser DPAPI blob. Empty `gemini_api_key` deletes the key. Responses,
settings JSON, and logs never contain the provider key.

Input language selection is authoritative: `ko`, `en`, `zh`, or `ja` is sent directly
to the local ASR backend and retained through translation and caption metadata, even if backend
metadata reports a conflicting language. `auto` passes no language to the local ASR and
retains its detected language. An explicit empty string, whitespace, or JSON null
in the `language` setting/start message means auto. Omitting the field preserves
the saved selection. Gemini uses the selected language as its documented language
hint; the local translation source label still respects the explicit selection.

`target_language` independently selects `ko` (default), `en`, `zh`, or `ja`.
It is accepted by settings, WS `start`, and POST `/v1/translate` alongside
`source_language`. Omission uses the saved target; explicit null/blank means `ko`.
`auto` is not an output language. Each session snapshots its target, so queued
translations and same-caption revisions cannot change language mid-run. Caption
events and history include `target_language`; older log rows default to `ko`.
All translation profiles use the selected target. Explicit/detected matching
source and target codes pass the source through without a translation request.
The auto text-script heuristic does not newly bypass English/Chinese/Japanese
translations. Character checks reject obvious wrong scripts but cannot prove
the language of Han-only Japanese/Chinese, Latin text, or semantic accuracy.

The Windows audio source list includes active WASAPI recording endpoints after
browser windows and the system mix. Microphone selection uses a stable endpoint
ID, not a display name or HWND. It opens a shared-mode capture endpoint without
the loopback flag and uses the same 16 kHz mono PCM16 conversion/100 ms transport
as existing sources. Disconnected, denied, or wrong-flow devices report errors;
they never fall back to system audio. Refresh preserves an available selected
device ID and clears a removed selection. Microphones have no browser window,
so the overlay uses its existing monitor fallback. The engine PCM API itself is
unchanged and does not receive/store the hardware device ID or recordings.

The normal path is settings -> prepare -> WS auth -> start -> PCM -> stop.
The settings API also accepts `asr_hints: list[str]` (default `[]`) and
`qwen_boundary_recheck: bool` (default `false`). Both apply only to local Qwen ASR;
boundary recheck is used only by the `legacy` profile.
Hints are user-entered source vocabulary, at most 32 entries, 48 characters per
entry and 512 retained characters total. Validation trims/NFC-normalizes terms,
removes blank/exact duplicate terms, and rejects controls/model special tokens.
Omission preserves saved values; `[]` clears hints; null/non-list/non-string
hints and non-boolean recheck values fail with HTTP 400. Active sessions reject
settings changes. No subtitle, transcript history, or translation glossary is
automatically inserted as ASR vocabulary. Hints do not guarantee recognition.
Boundary recheck is off by default; enable it when needed. When enabled, it adds
following audio to a forced-cut region for one more pass;
it can increase latency. The UI saves these options independently of model loading.
At the existing four-second forced boundary, the first ASR text is a raw preview.
The same audio start is retained with up to one more second of audio, or until
500 ms silence, and one recheck is finalized. Natural short utterances retain
their existing behavior. An empty/failed recheck finalizes the first result and
reprocesses the added PCM in the next window. Extra inference time means total
added latency is not bounded to one second.

`prepare` awaits model readiness and should receive a client timeout of at least
240 seconds on first load. WebSocket start also ensures readiness. No auth ACK is
sent; the first server event after start is status, followed by ready or error.
Local preparation runs a bounded ASR-only inference on one second of synthetic
audio before readiness. It never uses captured audio, performs translation, or
publishes the generated text as a transcript, caption, or history record. Qwen
and ordinary Whisper cap this disposable decode at two new tokens; AlignAtt uses
a separate temporary stream that is closed afterward. Successful warmup is cached
per loaded adapter (standard/AlignAtt), and unload invalidates it. Repeated prepare
does not warm the same loaded adapter again. Failure reports `asr_warmup_failed`;
`status.asr_warmup_ms` exposes its duration. This moves initial ASR work into prepare,
without promising a bound on the first real transcription or warming translation.

Use raw PCM16LE, mono, 16000 Hz; 100 ms messages (3200 bytes) are recommended.
Each binary message must contain at most 1 second and an even number of bytes.
The audio queue holds at most 192000 bytes and 120 messages. The translation queue
holds at most 6 sentence translations. Queue exhaustion emits an error and stops the
session, instead of silently accumulating stale subtitles.

The Windows client's `StreamingPcm16Converter` uses a streaming Blackman-windowed
sinc FIR/polyphase filter before downsampling to this format. Integer sample-phase
arithmetic preserves packet independence and prevents cumulative sample-count drift.
At 48 kHz the symmetric filter needs 2 ms lookahead (about 2.018 ms at 44.1 kHz);
16 kHz input bypasses it. This is additional input-availability delay, not a shift
or leading padding in exported PCM. WASAPI event delivery and the existing 100 ms
output packet assembly add their own delay. Finite export flushes the known filter
tail; a discontinuity finishes that tail and resets filter state without bridging
the gap. No automatic gain control is added. Engine-only tests that feed preconverted
PCM do not measure the real client's capture/filter/transport/display latency.

Whisper segments use a simple RMS input gate (0.002 normalized RMS, about -54 dBFS)
followed by the bundled Silero VAD. A 400 ms audio prefix preserves soft word onsets.
The deliberately permissive gate avoids discarding quiet speech before Silero can
inspect it; background noise may consequently reach the VAD more often. This is
not automatic gain control or an assurance of recognition accuracy at low volume.
Decoding uses beam size 5; a real speech check exposed an incorrect phrase with
greedy decoding that beam search recovered with a small latency increase.
Whisper uses `local_streaming.py`: approximately one second of new audio triggers
a new timed-word hypothesis. The normal path requires two consecutive hypotheses to agree before a
complete sentence or sufficiently long clause is committed. CJK-to-CJK phrase
spaces also form boundaries; ordinary Latin word spaces do not. Without a nearby
natural boundary, about three seconds of stable timed words may be published once
the unpublished phrase has waited 3.5 seconds. A middle fragment shorter than
2.5 seconds is held for more context; a whole stable short hypothesis can finish
after at least 800 ms beyond its last recognized word. A word pause of at least
600 ms can also end a short phrase. These are recognition-time boundaries, not
proof that a phrase is semantically complete.
ASR and translation take additional time; this is not a hard latency promise.
Time-based publication retains encoder audio context instead of immediately cutting
it away. Subsequent snapshots exclude only the already published, temporally aligned
prefix. CJK word resegmentation keeps the fresh suffix, and genuinely later repeated
speech remains present. Comparison keys equate Chinese simplified/traditional forms
and the 麽/么 variant on Windows without changing caption spelling or merging names
that merely sound alike. A matching preceding word and audio-time anchor can identify
a re-timed, already published last word. At eight seconds of buffered audio, safe old published word
boundaries may be trimmed while keeping at least three seconds of published context.
Natural boundaries clear retained context. `context_in_audio` suppresses a duplicate
Whisper text prompt. Silence finals are divided into short translation jobs too;
all source text remains present even when sentence punctuation is absent.
A 500 ms silence finalizes the remaining utterance. A bounded recovery path also
handles background music that never crosses the RMS silence threshold: old
ASR-empty audio is retired with recent context retained, and stalled hypotheses
can be finalized using the last recognition instead of waiting indefinitely.
Using an unconfirmed hypothesis emits the nonfatal `local_streaming_deadline`
warning. Recovery retains recent audio and resets stale text prompts when that
audio no longer contains the old context. It cannot recover words the recognizer
never detected or guarantee that an unstable hypothesis was correct.
Two consecutive nonempty hypotheses containing only already published context
retract an unsupported pending tail instead of unconditionally restoring it at a
deadline. This emits `local_streaming_pending_revised` as a nonfatal warning;
only the provisional caption may be removed, and committed captions stay intact.
Genuinely empty decoder responses retain the existing temporary-loss recovery path.
If the backend supplies only one growing segment with no internal word times,
the recovery may finish that entire segment at its observed audio end. In that
case only the still-unprocessed tail is retained; no character times are invented.
PCM remains bounded to 24 seconds as a final safeguard; genuine processing
overload still raises an explicit error. This is application-level LocalAgreement, not a native streaming Whisper
model.

Default Qwen (`legacy`, `qwen_boundary_recheck: false`) uses `FastQwenController`:
20 ms RMS frames at threshold 0.002 open windows with 400 ms pre-roll; at least
200 ms of above-threshold audio is required for recognition. It retains the
four-second maximum and 200 ms overlap only on forced cuts. PCM ingestion continues
while one immutable snapshot is recognized. Speculation requires both 200 ms of
actual RMS silence and a 200 ms endpoint hint. A neural-only non-speech prediction
over continuing background sound cannot trigger speculative ASR; the 500 ms neural
endpoint rule is unchanged. Early ASR neither translates nor publishes speculative text. At the
500 ms endpoint, nonempty cached text/language is reused only if the same window's
speech revision still matches. Any new RMS-positive audio invalidates that cache,
even if the neural detector calls it non-speech; empty speculative results require
a final decode. This avoids hiding newly arrived audio behind stale early text.

Optional Silero predictions can shorten an already opened window after speech has
been detected there. A negative neural prediction does not discard original PCM;
the accumulated waveform still reaches final ASR. Natural endpoints do not replay
RMS-positive audio as overlap. Neither silence nor a forced cut proves a semantic
sentence boundary. The Qwen adapter's whole-waveform speech check remains separate.
`qwen_boundary_recheck: true` selects the existing recheck path described above;
its default remains false. Inference/translation add latency, and user stop cancels
buffered work. Music and overlapping speakers still require real-broadcast evaluation.

For legacy Whisper, `local_captions.py` translates short pending phrases before ASR commitment.
The first preview requires at least 1.2 seconds of audio in the current window;
revisions require 0.8 seconds of both new audio and elapsed wall time. The preview
contains the first short phrase, capped at 96 source characters. Inference adds
latency, and these provisional Korean captions can change. Before its first accepted
translation offer, a short unpunctuated fragment needs two equal observations with
at least 350 ms of both new audio and elapsed wall time. The short-fragment rule
covers at most six alphanumeric characters when CJK is present, or at most two
words and 24 alphanumeric characters otherwise. Explicit sentence/clause endings,
authoritative finals, and corrections to an already accepted preview bypass this
extra stability wait. A deferred first offer retains the guard for changed text;
queue acknowledgement retries still respect the ordinary update throttle.
The first authoritative
segment replaces/finalizes the same ID; additional segments and real repetitions
get distinct IDs. Empty/retracted previews are removed without retracting commits.
Queue congestion suppresses optional local previews while retaining all definitive
translation jobs. In-flight obsolete results cannot overwrite newer revisions,
and final IDs cannot be downgraded by late previews. Identical final text reuses
the successful translation. A local preview quality failure retains the last
readable caption and retries on finalization; a failed definitive translation
still reports the existing failure caption/warning. Only successful final source
text is used as later translation context. Published provisional and definitive
captions upsert the same history row, with the existing latest-100 rotation.
Gemini keeps PCM continuous without client-forced stream ends.
Completed interim sentences observed twice and stable for 600 ms may be translated
provisionally. Authoritative finals replace the same caption IDs, preserve all final
sentences, and retract surplus provisional captions with `caption_remove`. Stale
in-flight translations are ignored after revision. A matching final reuses its
existing translation. Repeated speech in different finalized turns gets new IDs.
Both final and interim fields in a provider envelope are processed independently.

Stop is cancellation, not a final audio flush. Buffered and unfinished work is
discarded, and session identity prevents late translations from being displayed.
Synchronous Whisper calls may finish internally after cancellation; a thread lock
prevents another inference/unload from racing them. Models stay loaded after stop;
`POST /v1/release` stops active audio and frees the owned models/process.

The generic Qwen/chat profile disables thinking and requests the selected output language. A rejected
or empty output receives one fresh retry with stricter instructions and no prior
failed answer or source context. The HTTP translation endpoint reports content
failures explicitly. Streaming sessions instead preserve the source and emit a
Korean failure placeholder (`translation_status: failed`) and a nonfatal warning
for `translation_language`, `translation_empty`, or `translation_truncated`.
Recognition and subsequent sentences continue; a failed provisional translation
is retried when its authoritative final arrives, including a final received during
in-flight inference. Model/transport/protocol failures remain fatal. Failed outputs
are never used as successful translation cache or context. Input transcripts are
treated as quoted data. The model remains capable of mistakes; the language check
does not establish semantic translation accuracy.

MiLMMT uses a dedicated raw-completion profile, identified by its GGUF filename.
The installed `MiLMMT-46-12B-v1.0.i1-Q4_K_M.gguf` is a community quantization of
[Xiaomi's translation model](https://huggingface.co/xiaomi-research/MiLMMT-46-12B-v1.0).
It uses the official `Translate this from {source} to {target}:` prompt followed by
`{source}: {text}` and `{target}:`. No chat wrapper, thinking flag, past-source context,
or Hy-MT2 glossary is included. Decoding is greedy (temperature 0, top_k 1).
Only this profile uses physical prefill microbatches of 128 tokens to reduce
temporary CUDA buffers when local ASR is loaded alongside it; model precision
and the 4096-token context stay unchanged.
The pinned GGUF already has add_bos_token=false and EOS=1 (`<eos>`), matching the
original model's raw prompt contract. Use the original filename when importing.
The source model supports English, Japanese, Korean and both Chinese scripts;
the app's `zh` selection uses the official `Chinese (Simplified)` language name.
Model provenance and SHA-256 are recorded in `models/model-manifest.json`.
The model is subject to the [Gemma Terms of Use](https://ai.google.dev/gemma/terms).
Language-output checks do not measure semantic translation accuracy.

TranslateGemma 12B uses a separate profile when the GGUF file name identifies
TranslateGemma (case-insensitive; `translate-gemma` and `translate_gemma` are also
accepted). Keep the installed name `translategemma-12b-it-Q4_K_M.gguf` when importing
or selecting it from `models/translation`. Only files actually installed appear
in the list; all profiles use bundled llama.cpp and require no Ollama installation.

The TranslateGemma profile sends a raw Gemma user/model-turn prompt to `/completion`
for `source_lang -> target_language`, with only the current source sentence. It does not include
the generic system prompt, JSON message envelope, or previous translation context.
Explicit source-language selection takes precedence. When the language is `auto`
or absent, the character fallback checks kana, Han, then Hangul, and otherwise
uses English. Han-only Japanese is ambiguous with Chinese; select `ja` explicitly
for Japanese speech in that case. Same-language input is returned unchanged. Generation
uses 320 tokens, temperature 0 (0.2 on the single quality retry), and Gemma end tokens
as stop markers. Truncation and language failures follow the handling described above.
This profile and the language check do not guarantee semantic translation quality.

The previously installed Q4_K_M GGUF was from
[bullerwins/translategemma-12b-it-GGUF](https://huggingface.co/bullerwins/translategemma-12b-it-GGUF/tree/d7d1d8cc4ff53d4bc883ef33eae3894f07833b63),
based on [google/translategemma-12b-it](https://huggingface.co/google/translategemma-12b-it).
Its size is 7,300,793,664 bytes; the pinned revision and SHA-256 are recorded in
the historical `THIRD_PARTY_NOTICES.txt` entry at the app root.
The model is subject to the [Gemma Terms of Use](https://ai.google.dev/gemma/terms).

Hy-MT2 is selected by its GGUF file name, including `HY-MT2-7B-Q6_K.gguf`.
Place it in `models/translation` or import it, refresh the model list, and prepare it;
only installed files are selectable, with no Ollama needed.
The `/v1/chat/completions` request contains one user message with the source and
the official Chinese instruction for `zh`, or English instruction otherwise, targeting
the selected output language. It sends no system message or JSON source wrapper. Previously finalized source
sentences supply context: at most the latest three, with a 900-character combined
limit. Overlong context sentences are omitted whole rather than cut mid-sentence.
The current source is labeled separately and is the only text requested for translation.
Application instructions preserve politeness/formality and tone, prohibit invented
insults/facts/sentence endings, and request concise subtitles without explanation.
These instructions extend Tencent's official background, terminology, style, and
personalization examples; they are not a guarantee of correct output or ASR repair.

`config/translation-glossary.json` is a user-editable UTF-8 list of
`{"source":"师娘","target":"사모님"}` entries, reloaded during model preparation.
Its Korean target entries apply only when `target_language` is `ko`.
`load_glossary(path)` returns `(entries, warning)` instead of failing a subtitle session:
missing files are empty; unreadable, oversized, invalid-UTF-8/JSON, or non-list files
produce an empty glossary and a warning. Invalid entries and duplicate sources are
skipped while valid entries remain. The first duplicate wins. No file is modified.
Limits are 64 KiB, 200 catalog entries, and 64 characters per nonempty single-line
source/target. Only exact matches in the current source are included in a prompt,
at most 20, longest terms first. ASCII word boundaries prevent substring matches;
matching is case-sensitive and performs no simplified/traditional conversion or fuzzy
ASR correction. Control-token spellings are escaped in source, context, and terms.
Default name spellings are editable local conventions, not verified official credits.
The glossary is currently specific to Hy-MT2; the other model profiles are unchanged.

The [official 7B sampling settings](https://huggingface.co/tencent/Hy-MT2-7B#inference-and-deployment)
are temperature 0.7, top_p 0.6, top_k 20, and repeat_penalty 1.05. The app additionally
sets min_p=0 to disable llama.cpp's extra filter and caps output at 320 tokens.
It allows one quality retry, displays only content, and excludes reasoning content.

The pinned Tencent Q6_K artifact records EOS ID 3 (`$`). For the exact file name
`HY-MT2-7B-Q6_K.gguf`, the app applies
`--override-kv tokenizer.ggml.eos_token_id=int:127960` at server startup and stops
generation on `<|eos|>` or `<|extra_5|>`. This corrects runtime metadata in memory;
the downloaded bytes and their SHA-256 remain unchanged. The Apache-2.0 model's
pinned revision, size, and hash are recorded in `THIRD_PARTY_NOTICES.txt` and the
model manifest. These settings do not establish translation accuracy.

Live captions are logged to `logs/caption-history.json` as UTF-8 JSON. The file
keeps only the latest 100 records (newest first), with the original text,
translation, source/target languages, timestamp, and caption status. Corrections update the same record;
retracted provisional captions are removed. Logging continues across app restarts,
but old records are never restored to the app UI or sent to streaming clients.
File writes run separately from caption delivery; storage errors do not stop
subtitles. Windows access/sharing conflicts during atomic log replacement retry
the same operation up to three times (10/30/60 ms); permanent errors still report
the existing warning and preserve the previous log. No audio or credentials are included.

`POST /v1/logs/clear` is authenticated and native-only (no Origin). It deletes
regular files directly under the application's `logs` directory and reports
`{ok, deleted_count, failed_files}`. Nested directories and reparse/link targets
are not traversed. An ordered history barrier drains prior writes, clears the
file and in-memory rows, and then permits new captions to create a fresh log.
Retired caption IDs stay remembered so late revisions do not restore deleted
records. Active recognition and loaded models remain running. The Windows
button is labeled **번역 기록 삭제**; its success also clears the displayed history.
llama-server stdout is drained in bounded chunks by a background pipe reader.
Short-lived file opens share a lock with deletion, allowing Windows to delete
the log while inference continues. Log writes may be discarded during deletion
or a disk error; new output recreates the file. No weights/configuration are deleted.

Tests (no model weights or network needed):

```powershell
.\runtime\python\python.exe -m pytest engine\tests -q -p no:cacheprovider
```

Tests cover authentication, origin restrictions, single-session ownership,
cancellation/stale results, bounded queues, model traversal/import, DPAPI roundtrip,
Korean output retries, truncation, quiet-audio preservation, and ASR release when
changing to Gemini. Gemini transport tests replace the external WebSocket with a
fake provider and exercise setup, PCM transmission, interim/final transcripts,
translation dispatch, setup rejection, goAway, provider errors, and peer closure.
They establish protocol handling only; no real Gemini API key or external API call
is used, and provider availability/account permissions remain unverified.
The symlink-escape test skips when Windows denies symlink creation.

`requirements.txt` records the direct dependencies used in the bundled runtime;
pytest is a development dependency and is not required to run the service.
For a fresh source environment, run
`python engine/dependencies/build_pcm_whisper.py --install` from the app root.
It builds the hash-pinned `faster-whisper 1.2.1+livesubtitle.pcm1` dependency before
installing requirements. The live PCM path does not install PyAV or SoundFile.
The fork preserves the upstream Silero-v6 code and asset byte-for-byte; source,
license, offline setup, and release staging instructions are in
[`dependencies/faster_whisper_pcm/README.md`](dependencies/faster_whisper_pcm/README.md).

Provider protocol reference:
https://ai.google.dev/gemini-api/docs/live-api/live-transcribe
