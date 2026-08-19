# bluejay-aai-bridge

A WebSocket bridge that lets [Bluejay](https://getbluejay.ai) (over the
[CHIRP](https://docs.getbluejay.ai) protocol) run voice simulations against
the [AssemblyAI Voice Agent API](https://www.assemblyai.com/docs/voice-agents/voice-agent-api).

You bring your own system prompt — the bridge is a transport-only adapter.

## What it does

- Accepts a CHIRP WebSocket from Bluejay at `/voice` (HTTP Basic auth).
- Bridges to `wss://agents.assemblyai.com/v1/ws`.
- Resamples 16 kHz ↔ 24 kHz PCM with stdlib `audioop.ratecv` (stateful).
- Buffers inbound audio until AAI's `session.ready` (the API drops
  `input.audio` sent before it), then streams through.
- Translates CHIRP ↔ AAI events:
  - Bluejay binary frames → AAI `input.audio`.
  - AAI `reply.audio` → Bluejay binary frames.
  - First `reply.audio` of an utterance → CHIRP `speech.started`.
  - AAI `reply.done` → CHIRP `speech.completed`.
  - AAI `error` / `session.error` → CHIRP `session.error`.
- Ends sessions cleanly with `session.end` on hangup — skipping this
  leaves the session (and billing) alive for a 30-second resume window,
  which adds up fast across simulation runs.
- Optional live tool calls into the AssemblyAI docs MCP server
  (`search_docs`, `read_docs_page`).

## Configure your agent

**Option A — stored agent.** Create a reusable agent server-side
(`POST https://agents.assemblyai.com/v1/agents`) and set the
`AAI_AGENT_ID` env var. The bridge binds every session to it and all
inline config below is ignored. This is the recommended path if you
want to simulate against the exact agent you run in production.

**Option B — inline config.** Open [`agent_config.py`](agent_config.py)
and edit:

- **`SYSTEM_PROMPT_TEMPLATE`** — your agent's system prompt. Empty by
  default. Supports these format keys:
  - `{voice_name}` — randomly picked TTS voice for this session.
  - `{voice_accent}` — e.g. `American` / `British`.
  - `{voice_desc}` — short description of the voice.
  - `{current_datetime}` — current UTC datetime as a readable string.
- **`GREETING`** — what the agent says first, spoken verbatim by TTS
  (not run through the LLM). Empty by default (agent waits silently
  until the user speaks).
- **`KEYTERMS`** — up to 100 words to bias transcription toward (brand
  names, product names). Empty by default.
- **`VOICES`** — voice catalog used by `pick_voice()` for random
  per-session selection. Default is the eleven English voices; merge
  in `LANGUAGE_SPECIFIC_VOICES` (Italian, Spanish, German, Portuguese,
  French) or trim to a subset. `GET https://agents.assemblyai.com/v1/voices`
  is the authoritative live list.
- **`TOOLS`** — function tools registered on the AAI session. Default
  is the AssemblyAI docs MCP at `https://www.assemblyai.com/docs/mcp`
  (`search_docs`, `read_docs_page`). Set to `[]` if you don't want
  tool calling.
- **Speech-to-text / turn-taking tuning** (all optional, server
  defaults when unset):
  - `TRANSCRIPTION_MODE` — `balanced` (default) / `min_latency` /
    `max_accuracy`.
  - `TRANSCRIPTION_PROMPT` — vocabulary context for the transcriber
    (max 1750 chars), distinct from the system prompt.
  - `LANGUAGE_CODES` — steer STT toward known languages, e.g.
    `["en", "es"]`; empty for automatic detection with code-switching.
  - `VOICE_FOCUS` / `VOICE_FOCUS_THRESHOLD` — noise suppression
    (`near-field` / `far-field`, strength 0.0–1.0).
  - `TURN_DETECTION` — `vad_threshold`, `min_silence` / `max_silence`
    (ms), `interrupt_response` (barge-in on/off), `interruption_delay`
    (ms). Leave `None` for AAI's adaptive endpointing — setting the
    silence windows disables adaptive pacing for the session.
  - `VOLUME` — agent playback volume 0–100.

See the [session configuration reference](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-configuration)
for details on every field.

## Configure deployment

| Env var | Required | Notes |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | yes | Bearer token for upstream Voice Agent API. |
| `CHIRP_USER` | yes (prod) | Basic-auth user Bluejay sends. Skip for dev. |
| `CHIRP_PASS` | yes (prod) | Basic-auth password Bluejay sends. Skip for dev. |
| `AAI_AGENT_ID` | no | Bind sessions to a stored agent; inline config is then ignored. |
| `PORT` | auto | Railway injects. Defaults to 8767 locally. |
| `AAI_WS_URL` | no | Override upstream (e.g. EU endpoint `wss://agents.eu.assemblyai.com/v1/ws`). |

## Run locally

```sh
pip install -r requirements.txt
ASSEMBLYAI_API_KEY=sk_xxx python main.py
```

Then point Bluejay at `ws://localhost:8767/voice` (or `/`) with no auth.

With auth:

```sh
ASSEMBLYAI_API_KEY=sk_xxx CHIRP_USER=myuser CHIRP_PASS=mypass python main.py
```

## Deploy on Railway

1. Push this folder to a Git repo.
2. New Railway service → connect repo.
3. Set env vars: `ASSEMBLYAI_API_KEY`, `CHIRP_USER`, `CHIRP_PASS`.
4. Railway picks up `Procfile` (`web: python main.py`) and `runtime.txt`.
5. Use the generated Railway URL (replace `https://` with `wss://`)
   in your Bluejay agent config.

## Notes

- **Resampling** uses stdlib `audioop.ratecv` because Bluejay sends 10 ms
  frames (160 samples). Filter-based resamplers like
  `scipy.signal.resample_poly` introduce a transient on every chunk
  boundary at that size, which AAI's STT can't decode. `audioop.ratecv`
  is stateful — filter state is carried across calls so chunk boundaries
  don't produce artifacts. `audioop` was removed from the stdlib in
  Python 3.13; this project pins Python 3.12 (see `runtime.txt`).
- **Bluejay user `speech.started` is currently logged-only**. AAI's VAD
  barges in fine from the audio alone, and forwarding could cause
  double interrupts. Easy to wire through if simulations show issues.
