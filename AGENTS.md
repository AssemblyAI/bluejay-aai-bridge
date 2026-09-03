# Working on this repo

A bridge that lets [Bluejay](https://getbluejay.ai) run voice simulations against an AssemblyAI Voice Agent. An agent is one file in `agents/`; `publish.py` pushes it to the account; `bridge.py` is the WebSocket server Bluejay dials.

```
bridge.py                  the CHIRP <-> Voice Agent API bridge, what Bluejay connects to
call.py                    place a call to the bridge yourself, no Bluejay needed
sessions.py                recordings and transcripts after a call
agents/                    empty; an agent kept here, as the body of POST /v1/agents
lib.py                     env loading, JSONC parsing, AssemblyAI calls
publish.py                 python publish.py
import_agent.py            python import_agent.py <id>, an existing agent into a file
```

`agents/` ships empty on purpose. This repo does not define an agent: the one under test is the user's, given as `AGENT_ID` or imported into a file. Do not add example agents. The [Voice Agent starter](https://github.com/AssemblyAI/voice-agent-starter-python) is where worked examples of every parameter live.

`agents/`, `lib.py`, `publish.py` and `import_agent.py` come from the [Voice Agent starter](https://github.com/AssemblyAI/voice-agent-starter-python) and behave identically. Fixes to them belong there first, then here.

## Run

```sh
pip install -r requirements.txt
cp .env.example .env    # ASSEMBLYAI_API_KEY and AGENT_ID
python bridge.py
python call.py --seconds 12    # another terminal
```

Python 3.12+. `aiohttp` for the WebSocket server and client; everything else, `lib.py` and the three scripts around it included, is standard library.

## How it fits together

Agent files are API request bodies. If a field isn't in the [create-agent reference](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/create-agent), it doesn't belong in the file. They use `.jsonc` so each field can carry a comment and a doc link. `parse_jsonc` in `lib.py` strips comments and trailing commas before the file is sent.

Each agent file owns an id, stored as `AGENT_ID_<NAME>`. Unset, `publish_agent` sends `POST /v1/agents` and writes the returned id under that key. Set, it sends `PUT /v1/agents/{id}`. A bare `AGENT_ID` overrides every per-file key, and is the path most people take, since the agent under test usually already exists. `bridge.py` resolves an id through `stored_agent_id(AGENT)` at startup, publishes `agents/<AGENT>.jsonc` if `AGENT` names one, and otherwise exits telling the user how to point it at an agent. It never invents one.

The session message contains only `{ agent_id }`. Prompt, voice, tools and turn detection are read from the stored agent, which is why behaviour changes belong in the agent file rather than in the bridge.

## The bridge

Two protocols, both WebSocket. Bluejay speaks [CHIRP](https://docs.getbluejay.ai/simulation-integrations/websockets): 16 kHz mono pcm_s16le in binary frames, plus optional JSON text events. AssemblyAI speaks the [Voice Agent API](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/events-reference): 24 kHz mono pcm_s16le, base64 inside JSON events. `Bridge` is one instance per connection and owns both sockets.

Four things in it are load-bearing, and each exists because of a documented behaviour rather than a preference:

- **Reply audio is paced to real time**, at most `PLAYOUT_LEAD_S` ahead. The API sends replies faster than real time; forwarding straight through fills Bluejay's playback buffer, and an interruption then arrives too late to stop anything. On `reply.done` with `status: "interrupted"` the queue is dropped and the generation counter bumped, so in-flight frames are discarded.
- **Audio before `session.ready` is held**, newest second only. The API discards audio sent before the session is up and drops anything streamed faster than real time, so the buffer is capped rather than flushed as a burst.
- **Hanging up sends `session.end`** and waits for `session.ended`. Closing the socket alone leaves the session resumable, and billable, for 30 seconds.
- **Fatal upstream errors become a CHIRP `session.error`** and a `1011` close, so a bad key or an unknown agent id shows up in Bluejay's test result instead of looking like a silent hang-up.

Resampling is `audioop.ratecv`, which carries filter state across calls. A per-chunk polyphase resampler re-settles its filter on every 20 ms frame and the transcriber could not decode the result.

## Rules

- The agent under test belongs to the user. No example agents, no default prompt, no voice picked here, nothing in the session message but an agent id. Behaviour changes go in the dashboard or in the user's own `agents/*.jsonc`; transport goes in `bridge.py`; anything shared goes in `lib.py`.
- Keep `lib.py`, `publish.py`, `import_agent.py` and the agent files in step with the starter repo. They are a copy, not a fork.
- Prefer `http` tools. AssemblyAI executes those itself, so they work in a simulation with nothing to wire up. A tool without an `http` block reaches the bridge as `tool.call`, which it can only answer with an error; `publish.py` warns at publish time.
- Voices: only IDs from the documented catalog at https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices. Never invent one. The tests check this.
- Never log or commit the API key, `CHIRP_PASS`, or a Bluejay key. `.env` and `agents/*.env` are gitignored; keep them that way.
- Conversation content, transcripts and tool arguments alike, goes through `spoken()` and is gated on `LOG_TRANSCRIPTS`. It is untrusted text on its way to a log line: strip control characters so it cannot forge one, bound the length, and let a hosted bridge turn it off entirely.
- Only use documented events and endpoints, and keep the doc links accurate, since they are how anyone reading the repo finds the reference.
- Voice-first prompt style: short spoken sentences, no visual formatting, no exclamation marks.
- New agent file: name it after the parameter or integration it demonstrates, not the persona. Comment every non-obvious field with a link to the page that defines it, and add a row to `README.md` and `agents/README.md`.
- `python -m unittest discover -s tests` before pushing. The tests cover the audio path, CHIRP framing, and any agent file the user has added, and need no API key.
- `call.py` is the pre-flight check, not a second simulator. It exists so a deployment can be verified before a suite of paid simulations runs against it. Keep it faithful to the CHIRP spec: what it does is what Bluejay does.

## Reference

- [Create an agent](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/create-agent) · [Manage agents](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/manage-agents)
- [Events reference](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/events-reference) · [Message sequence](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/message-sequence) · [Audio format](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/audio-format)
- [Turn detection and interruptions](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/turn-detection-and-interruptions) · [Tools overview](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/overview) · [HTTP tools](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/http-tools)
- [Session history](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-history) · [Troubleshooting](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/troubleshooting)
- [Bluejay CHIRP protocol](https://docs.getbluejay.ai/simulation-integrations/websockets) · [Simulation tool calls](https://docs.getbluejay.ai/test/simulations/tool-calls)

## Deploying

Bluejay dials in, so the bridge has to be reachable at a public `wss://` address; a tunnel works for a trial. `render.yaml` and the `Procfile` cover Render and Railway, both prompting for `ASSEMBLYAI_API_KEY` and `AGENT_ID`. Never host it on an instance that sleeps when idle: simulations arrive in bursts after a quiet period, and the first calls of a run will time out on the upgrade. Anyone with the URL and the CHIRP credentials runs sessions billed to that key.
