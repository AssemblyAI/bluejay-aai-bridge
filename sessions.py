#!/usr/bin/env python3
"""What happened on a call, from AssemblyAI's side.

    python sessions.py                 # recent sessions
    python sessions.py sess_abc123     # download one and print its transcript

Every simulation Bluejay runs is a Voice Agent session with its own recording
and a turn-by-turn timeline, including tool calls and which replies were
interrupted. The bridge prints the session id when a call ends.

Artifacts land in out/<session_id>/: audio.ogg (stereo, Digital Human left and
agent right), timeline.json, metadata.json.

https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-history
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from lib import ApiError, aai, load_env, required

OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))


def list_sessions(limit: int = 20) -> None:
    query = f"/sessions?limit={limit}"
    if os.environ.get("AGENT_ID"):
        query += f"&agent_id={os.environ['AGENT_ID']}"
    sessions = aai(query).get("sessions", [])
    if not sessions:
        print("No sessions yet. Run a simulation first.")
        return
    for s in sessions:
        seconds = s.get("duration_seconds")
        print(f"{s['id']}  {s.get('status', ''):<10} "
              f"{f'{seconds:6.0f}s' if seconds is not None else '      -'}  "
              f"{s.get('created_at', '')}  agent={s.get('agent_id') or 'inline'}")
    print(f"\n  python sessions.py {sessions[0]['id']}")


def fetch(session_id: str) -> None:
    session = aai(f"/sessions/{session_id}")
    print(f"session   {session.get('id')}")
    print(f"status    {session.get('status')} "
          f"({session.get('public_close_reason') or 'no close reason'})")
    print(f"duration  {session.get('duration_seconds')}s   "
          f"agent {session.get('agent_id') or 'inline config'}")
    voice = ((session.get("config") or {}).get("output") or {}).get("voice")
    if voice:
        print(f"voice     {voice}")

    artifacts = session.get("artifacts") or []
    if not artifacts:
        print("\nNo artifacts yet. They appear once the session completes; try again shortly.")
        return

    out = OUT_DIR / session_id
    out.mkdir(parents=True, exist_ok=True)
    suffix = {"audio/ogg": "ogg", "application/json": "json"}
    timeline = None
    for artifact in artifacts:
        dest = out / f"{artifact['type']}.{suffix.get(artifact.get('content_type'), 'bin')}"
        # Pre-signed URL: the signature authorises it, so no Authorization header.
        with urllib.request.urlopen(artifact["url"], timeout=120) as res:
            dest.write_bytes(res.read())
        print(f"saved     {dest}")
        if artifact["type"] == "timeline":
            timeline = json.loads(dest.read_text())

    if timeline:
        print()
        print_transcript(timeline)


def print_transcript(timeline: dict) -> None:
    started = timeline.get("started_at_unix_ms")

    def at(ms: int) -> str:
        return f"+{(ms - started) / 1000:6.1f}s" if (ms and started) else "        "

    # Empty arrays are dropped from the JSON, so a call where nobody spoke has
    # no "turns" key at all and a turn with no tools has no "tool_calls".
    for turn in timeline.get("turns", []):
        if turn.get("user_transcript"):
            print(f"  {at(turn.get('user_speech_started_at_ms'))}  User:  {turn['user_transcript']}")
        for call in turn.get("tool_calls", []):
            result = " ".join(str(call.get("result", "")).split())[:100]
            failed = " (error)" if call.get("is_error") else ""
            print(f"  {at(call.get('dispatched_at_ms'))}  tool:  {call.get('name')}"
                  f"({json.dumps(call.get('arguments'))}) -> {result}{failed}")
        if turn.get("agent_text"):
            cut = " (interrupted)" if turn.get("status") == "interrupted" else ""
            print(f"  {at(turn.get('agent_reply_started_at_ms'))}  Agent: {turn['agent_text']}{cut}")


def main() -> None:
    load_env()
    required("ASSEMBLYAI_API_KEY",
             "cp .env.example .env and add your key from "
             "https://www.assemblyai.com/dashboard/api-keys")
    if len(sys.argv) > 1:
        fetch(sys.argv[1])
    else:
        list_sessions()


if __name__ == "__main__":
    try:
        main()
    except ApiError as err:
        sys.exit(str(err))
