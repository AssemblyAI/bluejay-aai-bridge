"""
Agent configuration — system prompt, voice catalog, tool definitions, and
session_config builder. Copied from the main web-agent-proxy repo so this
service can be deployed independently. When the upstream prompt changes,
this file should be re-synced.
"""

import json
import random
from datetime import datetime, timezone

from mcp import ClientSession


MCP_URL = "https://mcp.assemblyai.com/docs"


TOOLS = [
    {
        "type": "function",
        "name": "search_docs",
        "description": (
            "Search AssemblyAI documentation across all pages. Use whenever a "
            "user asks something factual about AssemblyAI products, APIs, SDKs, "
            "features, models, languages, pricing, or behavior that isn't "
            "already in your context. Returns relevant snippets and page paths."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "get_pages",
        "description": (
            "Retrieve full content of specific AssemblyAI documentation pages "
            "by path. Use after search_docs when a snippet isn't enough."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Page paths returned by search_docs.",
                },
            },
            "required": ["paths"],
        },
    },
    {
        "type": "function",
        "name": "list_sections",
        "description": (
            "Browse the structure of AssemblyAI documentation. Use when you're "
            "not sure what to search for."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "get_api_reference",
        "description": (
            "Get API endpoint details and schemas for AssemblyAI APIs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "endpoint": {
                    "type": "string",
                    "description": "Endpoint path or topic.",
                },
            },
        },
    },
]


async def execute_mcp_tool(mcp: ClientSession, event: dict) -> dict:
    """Forward a Voice Agent tool.call to the AssemblyAI docs MCP server."""
    name = event.get("name", "")
    args = event.get("arguments", event.get("args", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    is_error = False
    try:
        mcp_result = await mcp.call_tool(name, args)
        text = "\n".join(c.text for c in mcp_result.content if getattr(c, "text", None))
        result = text or "No content returned."
    except Exception as e:
        result = f"Error calling {name}: {e}"
        is_error = True
    print(f"  Tool: {name}({json.dumps(args)[:80]}) → {result[:120]}")
    return {"call_id": event.get("call_id", ""), "result": result, "is_error": is_error}


# Current documented voice catalog. GET https://agents.assemblyai.com/v1/voices
# is the authoritative live list.
VOICES = {
    # English — US
    "alba":    {"accent": "American", "desc": "American-accented English"},
    "eve":     {"accent": "American", "desc": "American-accented English"},
    "george":  {"accent": "American", "desc": "American-accented English"},
    "jane":    {"accent": "American", "desc": "American-accented English"},
    "jean":    {"accent": "American", "desc": "American-accented English"},
    "mary":    {"accent": "American", "desc": "American-accented English"},
    "michael": {"accent": "American", "desc": "American-accented English"},
    # English — UK
    "anna":    {"accent": "British",  "desc": "British-accented English"},
    "charles": {"accent": "British",  "desc": "British-accented English"},
    "paul":    {"accent": "British",  "desc": "British-accented English"},
    "vera":    {"accent": "British",  "desc": "British-accented English"},
}

# Voices with a native non-English accent that code-switch naturally with
# English. Merge any of these into VOICES to include them in the random pick.
LANGUAGE_SPECIFIC_VOICES = {
    "giovanni": {"accent": "Italian",     "desc": "native Italian accent, code-switches with English"},
    "lola":     {"accent": "Spanish",     "desc": "native Spanish accent, code-switches with English"},
    "juergen":  {"accent": "German",      "desc": "native German accent, code-switches with English"},
    "rafael":   {"accent": "Portuguese",  "desc": "native Portuguese accent, code-switches with English"},
    "estelle":  {"accent": "French",      "desc": "native French accent, code-switches with English"},
}


def pick_voice() -> str:
    """Pick a random voice for this session."""
    return random.choice(list(VOICES.keys()))


SYSTEM_PROMPT_TEMPLATE = """\
# Configure your own agent's system prompt here.
#
# This bridge does not ship with a default prompt. Drop whatever
# personality, rules, and behaviour you want the agent to have.
#
# The following format keys are interpolated at session start in
# session_config() below — use them in your prompt or remove them:
#   {voice_name}       — randomly picked TTS voice for this session
#   {voice_accent}     — e.g. "American" / "British"
#   {voice_desc}       — short description of the voice's personality
#   {current_datetime} — current UTC datetime as a readable string
"""


GREETING = ""  # Spoken verbatim by TTS at session start (NOT run through the
               # LLM). Leave empty for the agent to stay silent until the
               # user speaks first. Immutable after session.ready.

KEYTERMS: list[str] = []  # Up to 100 words/phrases to bias transcription
                          # toward, e.g. brand or product names. Optional.

# --- Optional speech-to-text / turn-taking tuning --------------------------
# All of these default to the server's behavior when left unset. See
# https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-configuration

# STT speed vs. accuracy: "balanced" (default), "min_latency", "max_accuracy".
TRANSCRIPTION_MODE: str | None = None

# Plain-language description of expected vocabulary to bias transcription
# (max 1750 chars). Distinct from the LLM system prompt.
TRANSCRIPTION_PROMPT: str | None = None

# Steer STT toward known language(s), e.g. ["en"] or ["en", "es"]. Leave
# empty for automatic multilingual detection with native code-switching.
LANGUAGE_CODES: list[str] = []

# Noise suppression model: "near-field" (default) or "far-field", with
# strength 0.0-1.0 (default 0.7). Leave VOICE_FOCUS as None for the default.
VOICE_FOCUS: str | None = None
VOICE_FOCUS_THRESHOLD: float | None = None

# Turn detection / barge-in tuning. Leave as None for adaptive defaults
# (recommended — setting min_silence/max_silence disables adaptive pacing
# for the session). Example:
#   TURN_DETECTION = {
#       "vad_threshold": 0.5,        # 0.0-1.0, lower = more sensitive
#       "min_silence": 1000,         # ms of silence for confident end-of-turn
#       "max_silence": 3000,         # ms of silence to force end-of-turn
#       "interrupt_response": True,  # False disables barge-in entirely
#       "interruption_delay": 500,   # ms (0-1000) before barge-in can interrupt
#   }
TURN_DETECTION: dict | None = None

# Agent playback volume, 0 (silent) to 100 (loudest). None = native level.
VOLUME: int | None = None


def session_config(voice: str | None, agent_id: str | None = None) -> dict:
    """Build the first session.update payload for AssemblyAI Voice Agent API.

    If agent_id is set, the session binds to a stored agent (created via
    POST https://agents.assemblyai.com/v1/agents) whose config lives
    server-side — all inline settings in this file are ignored.

    Otherwise, edit SYSTEM_PROMPT_TEMPLATE, GREETING, KEYTERMS, and the
    tuning knobs above to configure the agent inline.
    """
    if agent_id:
        return {"type": "session.update", "session": {"agent_id": agent_id}}

    info = VOICES[voice]
    now = datetime.now(timezone.utc)
    current_datetime = now.strftime("%A, %B %-d, %Y at %-I:%M %p UTC")
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        voice_name=voice,
        voice_accent=info["accent"],
        voice_desc=info["desc"],
        current_datetime=current_datetime,
    )
    input_cfg: dict = {"type": "audio"}
    if KEYTERMS:
        input_cfg["keyterms"] = KEYTERMS
    if TRANSCRIPTION_MODE:
        input_cfg["transcription_mode"] = TRANSCRIPTION_MODE
    if TRANSCRIPTION_PROMPT:
        input_cfg["transcription_prompt"] = TRANSCRIPTION_PROMPT
    if LANGUAGE_CODES:
        input_cfg["language_codes"] = LANGUAGE_CODES
    if VOICE_FOCUS:
        input_cfg["voice_focus"] = VOICE_FOCUS
        if VOICE_FOCUS_THRESHOLD is not None:
            input_cfg["voice_focus_threshold"] = VOICE_FOCUS_THRESHOLD
    if TURN_DETECTION:
        input_cfg["turn_detection"] = TURN_DETECTION

    output_cfg: dict = {"type": "audio", "voice": voice}
    if VOLUME is not None:
        output_cfg["volume"] = VOLUME

    session: dict = {
        "system_prompt": system_prompt,
        "tools": TOOLS,
        "input": input_cfg,
        "output": output_cfg,
    }
    if GREETING:
        session["greeting"] = GREETING
    return {"type": "session.update", "session": session}
