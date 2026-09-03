[![Voice Agent API](https://img.shields.io/badge/docs-Voice%20Agent%20API-2545E6)](https://www.assemblyai.com/docs/voice-agents/voice-agent-api)
[![Bluejay](https://img.shields.io/badge/docs-CHIRP%20protocol-1d4ed8)](https://docs.getbluejay.ai/simulation-integrations/websockets)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.12-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![AssemblyAI Twitter](https://img.shields.io/twitter/follow/AssemblyAI?label=%40AssemblyAI&style=social)](https://twitter.com/AssemblyAI)

# Bluejay simulations for AssemblyAI Voice Agents

[Bluejay](https://getbluejay.ai) runs simulated callers, Digital Humans, against a voice agent and scores what happens: happy paths, interruptions, accents, the edge cases you would never sit through by hand. It reaches an agent over [CHIRP](https://docs.getbluejay.ai/simulation-integrations/websockets), its own WebSocket protocol. This repo is the piece in between, translating CHIRP to the [AssemblyAI Voice Agent API](https://www.assemblyai.com/products/voice-agent-api) so those simulations run against the agent you actually ship.

```
Bluejay ──CHIRP/WebSocket──▶ bridge.py ──Voice Agent API──▶ your agent
        16 kHz PCM + events            24 kHz PCM + JSON events
```

Nothing here defines an agent. There are no examples to delete and no prompt to override: you give the bridge the id of an agent you already run, and its prompt, voice, tools and turn detection all come from that stored agent. The bridge is transport only.

## Quickstart

### 1. Clone

```sh
git clone https://github.com/AssemblyAI/bluejay-aai-bridge
cd bluejay-aai-bridge
pip install -r requirements.txt
cp .env.example .env
```

### 2. Add your key and your agent

The key is at [assemblyai.com/dashboard/api-keys](https://www.assemblyai.com/dashboard/api-keys). The agent id is in the agent's URL in the [dashboard](https://www.assemblyai.com/dashboard).

```sh
# .env
ASSEMBLYAI_API_KEY=your_key_here
AGENT_ID=7ad24396-b822-4dca-871a-be9cc4781cf9
```

That is the whole setup. If you would rather keep the agent in the repo, as a file you can edit between simulation runs, pull it in and publish it back when you change it. See [agents/](agents/).

```sh
python import_agent.py <agent-id>     # writes agents/<its-name>.jsonc
AGENT=<name> python bridge.py
```

### 3. Call it yourself first

```sh
python bridge.py
python call.py --seconds 12    # in another terminal
```

`call.py` speaks CHIRP the way Bluejay does, so a call that sounds right through it will sound right in a simulation. It records the agent to `out/agent.wav`. Give it something to say and it will hold a conversation:

```sh
say -o q.aiff "Hi, I have a question about my order" \
  && afconvert -f WAVE -d LEI16@16000 -c 1 q.aiff question.wav
python call.py --wav question.wav --seconds 30
```

Lower `--delay` so the question starts while the greeting is still playing, and you have tested barge-in.

### 4. Host it

Bluejay dials in, so the bridge needs a public `wss://` address. See [Hosting](#hosting), then set the credentials Bluejay will send:

```sh
# .env, and the same values on the host
CHIRP_USER=bluejay
CHIRP_PASS=a-long-random-string
```

### 5. Point Bluejay at it

In Bluejay, create an Agent with connection type **Websocket**, URL `wss://<your-host>/voice`, and that same user and password. Run a simulation.

---

## Hosting

Any host that runs Python, terminates TLS, and passes WebSockets through will do. Two are set up here.

### Railway

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new/template?template=https%3A%2F%2Fgithub.com%2FAssemblyAI%2Fbluejay-aai-bridge)

Or from an existing project: **New** → **GitHub Repo** → this repo. Railway reads [.python-version](.python-version) and [requirements.txt](requirements.txt) to build, and the [Procfile](Procfile) for the start command. Under **Variables** set `ASSEMBLYAI_API_KEY`, `AGENT_ID`, `CHIRP_USER` and `CHIRP_PASS`; `PORT` arrives on its own. Then under **Settings** → **Networking** generate a domain, and give Bluejay `wss://<that domain>/voice`.

### Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/AssemblyAI/bluejay-aai-bridge)

Render reads [render.yaml](render.yaml). It prompts for `ASSEMBLYAI_API_KEY` and `AGENT_ID`, generates a `CHIRP_PASS` for you to copy into Bluejay, and sets `PORT` itself.

### Anywhere else

```sh
pip install -r requirements.txt
python -u bridge.py
```

One stateless process, no database, nothing shared between calls, so it scales by running more of them. `GET /health` returns the agent id it resolved, which is what to point a health check at.

Two things to watch, both of which cost you failed simulations rather than errors you can see:

- **Do not run it on an instance that sleeps when idle.** Simulations arrive in bursts after a long quiet period, which is exactly when a scaled-to-zero instance is cold. The first calls of the run time out on the WebSocket upgrade, and Bluejay records them as `INCOMPLETED`.
- **Concurrent simulations need concurrent sessions.** Each call is one Voice Agent session on your account. Past the limit AssemblyAI returns `concurrency_exceeded`, which the bridge passes to Bluejay as a failed call.

To try it before deploying anything, put a tunnel in front of it. Bluejay only needs a reachable `wss://` URL:

```sh
python bridge.py
cloudflared tunnel --url http://localhost:8767    # or: ngrok http 8767
```

## The agent under test

The bridge resolves one agent at startup, prints it, and binds every call to it. `AGENT_ID` is the direct route. `AGENT=<name>` publishes `agents/<name>.jsonc` first and uses that, which is worth doing when you want the agent changing under version control alongside the simulation results it produced.

```sh
AGENT_ID=7ad24396-... python bridge.py     # an agent you keep elsewhere
AGENT=support-line python bridge.py        # an agent you keep here
```

Because the session message carries only an agent id, nothing about the agent's behaviour can be set from this repo's code. Change it in the dashboard, or in its file followed by `python publish.py`. See [agents/](agents/).

## Configuration

| Variable | | |
| --- | --- | --- |
| `ASSEMBLYAI_API_KEY` | required | Sessions are billed to it. Stays in this process. |
| `AGENT_ID` | one of these | The agent to test. Connected to as it is. |
| `AGENT` | | Which file in `agents/` to publish and test instead. |
| `AGENT_ID_<NAME>` | | The id `python publish.py` saved for that file. Set for you. |
| `CHIRP_USER`, `CHIRP_PASS` | before hosting | The Basic-auth pair Bluejay sends. Unset means anyone who can reach the port can start a call on your key. |
| `BLUEJAY_API_KEY` | optional | Writes each call's AssemblyAI session id back onto the Bluejay simulation result. |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every event and the level of the incoming audio. |
| `LOG_TRANSCRIPTS` | off | Print what was said. `.env.example` sets it to `1` so local runs show the conversation. Leave it out when hosting, since those logs go to a third party; turn counts are logged either way. |
| `PORT` | `8767` | Set for you by Railway and Render. |

A `.env` next to `bridge.py` is loaded on startup, and real environment variables win over it. [.env.example](.env.example) lists them all.

## After a simulation

Bluejay has the transcript, the recording and the evaluations. AssemblyAI has its own record of the same call, with a stereo recording, per-turn timings, and which replies were cut off:

```sh
python sessions.py                 # recent sessions
python sessions.py sess_abc123     # download one and print its transcript
```

The bridge prints the session id when each call ends. With `BLUEJAY_API_KEY` set, that id is also written onto the Bluejay result, so the two records point at each other. See [Recordings and transcripts](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-history).

## How the two protocols meet

| Bluejay (CHIRP) | | AssemblyAI Voice Agent API |
| --- | --- | --- |
| upgrade with `Authorization: Basic` and `X-Simulation-Result-Id` | ▶ | connect to `/v1/ws` with the API key, then `session.update` with `{agent_id}` |
| binary frame, 16 kHz pcm_s16le | ▶ | `input.audio`, base64 at 24 kHz. Held until `session.ready`, which the API needs before it will accept audio. |
| `speech.started` / `speech.completed` | ▶ | logged. The agent's own turn detection works from the audio. |
| `speech.started {utterance_id}` | ◀ | the first `reply.audio` of a reply |
| binary frames, 20 ms, at real-time pace | ◀ | `reply.audio` chunks |
| `speech.completed`, then `mark` | ◀ | `reply.done` with `status: "completed"`, once the last frame is out |
| `speech.completed` at once, queued audio dropped | ◀ | `reply.done` with `status: "interrupted"` |
| `session.error`, then close `1011` | ◀ | a connection-level `error`, a `session.error` before the session is up, or an unexpected close |
| close `1000` | ▶ | `session.end`, then wait for `session.ended` |

Two details are worth knowing, because both are the difference between a simulation that measures your agent and one that measures the bridge.

**Reply audio is paced.** The API sends a reply faster than real time. Forwarding it straight through would put seconds of speech in Bluejay's playback buffer, and an interruption would arrive to find the agent already committed to talking. The bridge stays at most 200 ms ahead, so `reply.done` with `status: "interrupted"` actually stops the voice.

**Hanging up ends the session.** When Bluejay closes the call the bridge sends `session.end` and waits for `session.ended`. Dropping the socket instead leaves the session resumable, and billable, for another 30 seconds, which over a suite of simulations is real money.

## Build with AI coding agents

This repo includes [AGENTS.md](AGENTS.md), which Claude Code, Cursor and Copilot read for its conventions. The Voice Agent API changes, so point coding tools at the current documentation rather than letting them work from memory:

> Always fetch https://assemblyai.com/docs/llms.txt before writing AssemblyAI code. The API has changed, do not rely on memorized parameter names.

```sh
claude mcp add --transport http --scope user assemblyai-docs https://mcp.assemblyai.com/docs
```

See [Build with AI tools](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/build-with-ai-tools).

## Voice Agent API

Product: [Voice Agent API](https://www.assemblyai.com/products/voice-agent-api) · [Pricing](https://www.assemblyai.com/pricing) · [Dashboard](https://www.assemblyai.com/dashboard)

Start here: [Documentation](https://www.assemblyai.com/docs/voice-agents/voice-agent-api) · [Create an agent](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/create-agent) · [Manage agents](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/manage-agents) · [Prompting guide](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/prompting-guide) · [Best practices](https://www.assemblyai.com/docs/voice-agents/best-practices)

Configuration: [Voices](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices) · [Greeting](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/greeting) · [Turn detection](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/turn-detection-and-interruptions) · [Keyterms](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/transcription-prompt) · [Languages](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/supported-languages) · [Noise suppression](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/noise-suppression)

Tools: [Overview](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/overview) · [HTTP tools](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/http-tools) · [Client-side tools](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/client-side-tools)

Reference: [Session configuration](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-configuration) · [Events](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/events-reference) · [Message sequence](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/message-sequence) · [Session history](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-history) · [Troubleshooting](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/troubleshooting)

Bluejay: [WebSocket simulations](https://docs.getbluejay.ai/simulation-integrations/websockets) · [Simulation tool calls](https://docs.getbluejay.ai/test/simulations/tool-calls) · [Digital Humans](https://docs.getbluejay.ai/key-concepts/digital-humans/overview)

## Cost

Every simulated call is a Voice Agent session billed to the API key in your `.env`, and Bluejay bills its own side. Running a suite costs real money, so keep `CHIRP_USER` and `CHIRP_PASS` set on anything hosted: without them, anyone who finds the URL can start sessions on your key.
