# agents/

Empty on purpose. There are no example agents in this repo, because the agent worth simulating is the one you already run.

You do not have to put anything here at all. If your agent lives in the AssemblyAI dashboard, give the bridge its id and skip this directory:

```sh
# .env
AGENT_ID=7ad24396-b822-4dca-871a-be9cc4781cf9
```

## Keeping the agent as a file

Useful when you want to change the agent between simulation runs, review those changes, or keep them next to the results they produced.

```sh
python import_agent.py <agent-id>     # writes agents/<its-name>.jsonc
AGENT=<name> python bridge.py
```

Edit the file, run `python publish.py`, and the next simulation picks up the change. The first publish of a new file creates an agent and records its id in `.env` as `AGENT_ID_<NAME>`; later ones update that same agent, so each file keeps its own and switching between them overwrites nothing.

## What goes in a file

The JSON is the request body for [`POST /v1/agents`](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/create-agent), with no wrapper fields and no keys this repo invents. If a field is not in that reference, it does not belong in the file. The same format as the [Voice Agent starter](https://github.com/AssemblyAI/voice-agent-starter-python), which has worked examples of every parameter if you want somewhere to start.

The `.jsonc` extension is so each field can carry a comment and a link to the page defining it. Comments and trailing commas are removed before the file is sent.

```jsonc
{
  "name": "Support line",
  "system_prompt": "You are a support agent. Keep replies to one or two sentences.",
  "voice": { "voice_id": "anna" },
  "greeting": "Support here, what can I do for you?"
}
```

Credentials never go in the JSON. Write `${MY_KEY}` anywhere in the file and put the value in `.env`, or in `agents/<name>.env` if only this agent uses it. Both are gitignored, so the file itself is safe to commit. A missing variable stops the publish and names it.

```jsonc
"headers": [{ "name": "x-api-key", "value": "${MY_KEY}" }]
```

Header values and LLM keys are write-only on the API, so they do not come back with an imported agent. `import_agent.py` says which ones to restore.

## Tools

Prefer tools with an `http` block. AssemblyAI makes those requests itself, so they work during a simulation with nothing to wire up, and the bridge only sees a notification that one fired.

A tool without an `http` block has to be answered by whoever holds the session. The bridge is transport only, so it replies with an error rather than letting the call stall until the tool times out, and `python publish.py` warns when you publish one. If a simulation needs a tool stubbed rather than really called, that is the hook to add: `on_tool_call` in [bridge.py](../bridge.py).

## Documentation

[Create an agent](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/create-agent) · [Voices](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices) · [Greeting](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/greeting) · [Turn detection](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/turn-detection-and-interruptions) · [Keyterms](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/transcription-prompt) · [HTTP tools](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/http-tools) · [Prompting guide](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/prompting-guide)
