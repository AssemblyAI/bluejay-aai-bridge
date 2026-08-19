#!/usr/bin/env python3
"""
Bluejay (CHIRP protocol) <-> AssemblyAI Voice Agent API bridge.

Bluejay opens a WebSocket TO this server. We bridge the conversation through
to AssemblyAI's Voice Agent API using the same agent config as the production
homepage agent (see agent_config.py).

Protocol notes:
  - Bluejay speaks 16 kHz mono pcm_s16le (binary) plus optional CHIRP text
    events: speech.started, speech.completed, session.error.
  - AssemblyAI Voice Agent API speaks 24 kHz mono pcm_s16le wrapped in JSON
    (input.audio inbound, reply.audio outbound).
  - We resample 16k <-> 24k with stdlib audioop.ratecv (stateful across
    chunks) and translate event shapes.
  - HTTP Basic auth on the upgrade per CHIRP. Set CHIRP_USER and CHIRP_PASS
    env vars to enable; leave unset to skip auth (dev only).
"""

import asyncio
import audioop  # stdlib; removed in Python 3.13 — this project pins 3.12
import base64
import json
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Tuple

import aiohttp
import numpy as np
from aiohttp import web, WSMsgType

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_config import (
    MCP_URL,
    execute_mcp_tool,
    pick_voice,
    session_config,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", os.getenv("API_KEY", ""))
AAI_WS_URL = os.getenv("AAI_WS_URL", "wss://agents.assemblyai.com/v1/ws")
# Optional: bind sessions to a stored agent (POST /v1/agents) instead of the
# inline config in agent_config.py.
AAI_AGENT_ID = os.getenv("AAI_AGENT_ID", "")
PORT = int(os.getenv("PORT", 8767))

# HTTP Basic auth on the WS upgrade (per CHIRP spec). If unset, the server
# accepts any connection — dev only, do not deploy without these set.
CHIRP_USER = os.getenv("CHIRP_USER", "")
CHIRP_PASS = os.getenv("CHIRP_PASS", "")

# Audio constants — Bluejay 16k mono pcm_s16le, AAI 24k mono pcm_s16le.
BLUEJAY_RATE = 16_000
AAI_RATE = 24_000
SAMPLE_BYTES = 2  # int16


# ---------------------------------------------------------------------------
# Audio helpers — streaming rate conversion with audioop.ratecv.
#
# scipy.signal.resample_poly was attempted first but produced unusable audio
# for AAI's STT: it applies a polyphase FIR filter that introduces a fresh
# transient on every chunk boundary, and Bluejay sends 10 ms chunks (160
# samples at 16 kHz) — far too small for the filter to settle independently
# per chunk. audioop.ratecv carries state across calls, eliminating the
# boundary artifacts.
# ---------------------------------------------------------------------------


def upsample_16_to_24(pcm16k: bytes, state) -> Tuple[bytes, object]:
    """Resample 16 kHz int16 mono → 24 kHz int16 mono. State must be threaded
    across calls in the same session."""
    if not pcm16k:
        return b"", state
    return audioop.ratecv(pcm16k, 2, 1, 16_000, 24_000, state)


def downsample_24_to_16(pcm24k: bytes, state) -> Tuple[bytes, object]:
    """Resample 24 kHz int16 mono → 16 kHz int16 mono. State must be threaded
    across calls in the same session."""
    if not pcm24k:
        return b"", state
    return audioop.ratecv(pcm24k, 2, 1, 24_000, 16_000, state)


# ---------------------------------------------------------------------------
# CHIRP event helpers
# ---------------------------------------------------------------------------


def make_chirp(event_type: str, data: dict) -> str:
    """Build a CHIRP text frame (UTF-8 JSON)."""
    return json.dumps({
        "type": event_type,
        "id": str(uuid.uuid4()),
        "ts_ms": int(time.time() * 1000),
        "data": data,
    })


def expected_basic_auth() -> Optional[str]:
    if not (CHIRP_USER and CHIRP_PASS):
        return None
    creds = f"{CHIRP_USER}:{CHIRP_PASS}".encode()
    return "Basic " + base64.b64encode(creds).decode()


# ---------------------------------------------------------------------------
# WebSocket bridge
# ---------------------------------------------------------------------------


async def bluejay_handler(request: web.Request) -> web.WebSocketResponse:
    """Accept a CHIRP WebSocket from Bluejay, bridge to AssemblyAI."""
    expected = expected_basic_auth()
    if expected is not None:
        if request.headers.get("Authorization") != expected:
            print("CHIRP auth rejected")
            return web.Response(status=401, text="Unauthorized")

    bluejay_ws = web.WebSocketResponse()
    await bluejay_ws.prepare(request)
    print("Bluejay connected")

    transcript: list = []
    session_start = datetime.now(timezone.utc)
    session_id: Optional[str] = None
    current_utterance_id: Optional[str] = None

    headers = {"Authorization": f"Bearer {ASSEMBLYAI_API_KEY}"}

    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(AAI_WS_URL, headers=headers) as aai_ws:
                # Send session.update FIRST so the greeting fires fast.
                if AAI_AGENT_ID:
                    print(f"  Stored agent: {AAI_AGENT_ID}")
                    await aai_ws.send_json(session_config(None, agent_id=AAI_AGENT_ID))
                else:
                    voice = pick_voice()
                    print(f"  Voice: {voice}")
                    await aai_ws.send_json(session_config(voice))

                # MCP setup runs as a background task — never blocks audio.
                mcp_holder: dict = {"client": None}
                mcp_ready = asyncio.Event()
                mcp_shutdown = asyncio.Event()

                async def mcp_keeper():
                    try:
                        async with streamable_http_client(MCP_URL) as (mr, mw), \
                                   ClientSession(mr, mw) as client:
                            await client.initialize()
                            mcp_holder["client"] = client
                            mcp_ready.set()
                            print("  MCP connected")
                            await mcp_shutdown.wait()
                    except Exception as e:
                        print(f"MCP setup failed: {e}")
                        mcp_ready.set()  # unblock so tool calls fail fast

                mcp_task = asyncio.create_task(mcp_keeper())
                pending_tool_tasks: list[asyncio.Task] = []

                async def run_tool(event: dict) -> dict:
                    await mcp_ready.wait()
                    client = mcp_holder["client"]
                    if client is None:
                        return {
                            "call_id": event.get("call_id", ""),
                            "result": "Docs lookup is currently unavailable.",
                        }
                    return await execute_mcp_tool(client, event)

                # Set when the agent is idle (not mid-reply). Used to wait
                # for any in-flight utterance before tearing down on hangup.
                agent_idle = asyncio.Event()
                agent_idle.set()

                # AAI drops input.audio sent before session.ready — buffer
                # until then. Set once the server acks the session config.
                session_ready = asyncio.Event()
                # Set when the server emits session.ended (clean teardown).
                session_ended = asyncio.Event()

                async def safe_send_text(payload: str):
                    if bluejay_ws.closed:
                        return
                    try:
                        await bluejay_ws.send_str(payload)
                    except Exception:
                        pass

                async def safe_send_bytes(payload: bytes):
                    if bluejay_ws.closed or not payload:
                        return
                    try:
                        await bluejay_ws.send_bytes(payload)
                    except Exception:
                        pass

                async def bluejay_to_aai():
                    """Pump from Bluejay (16k binary, optional CHIRP text) to AAI."""
                    binary_count = 0
                    binary_bytes = 0
                    last_logged_at = 0
                    window_peak = 0  # max absolute sample value since last log
                    window_sum_sq = 0.0  # for RMS
                    window_sample_count = 0
                    other_types: dict = {}
                    upsample_state = None  # carried across audioop.ratecv calls
                    # 24k audio that arrived before session.ready, flushed on
                    # first frame after. Bounded to ~10s (agent_timeout fires
                    # at 10s if the session never comes up).
                    pre_ready_buffer: deque = deque(maxlen=1000)
                    try:
                        async for msg in bluejay_ws:
                            if msg.type == WSMsgType.BINARY:
                                binary_count += 1
                                binary_bytes += len(msg.data)
                                # Inspect amplitude of the raw 16k frame so
                                # we can tell silence from speech.
                                in_samples = np.frombuffer(msg.data, dtype=np.int16)
                                if in_samples.size:
                                    peak = int(np.abs(in_samples).max())
                                    if peak > window_peak:
                                        window_peak = peak
                                    window_sum_sq += float((in_samples.astype(np.float32) ** 2).sum())
                                    window_sample_count += in_samples.size
                                pcm24k, upsample_state = upsample_16_to_24(msg.data, upsample_state)
                                if pcm24k:
                                    if not session_ready.is_set():
                                        pre_ready_buffer.append(pcm24k)
                                    else:
                                        while pre_ready_buffer:
                                            await aai_ws.send_json({
                                                "type": "input.audio",
                                                "audio": base64.b64encode(pre_ready_buffer.popleft()).decode(),
                                            })
                                        await aai_ws.send_json({
                                            "type": "input.audio",
                                            "audio": base64.b64encode(pcm24k).decode(),
                                        })
                                # Log every ~1s of audio with peak/RMS so we
                                # can tell silence from speech at a glance.
                                if binary_count - last_logged_at >= 50:
                                    rms = (window_sum_sq / window_sample_count) ** 0.5 if window_sample_count else 0.0
                                    print(
                                        f"  Bluejay audio: {binary_count} frames, "
                                        f"{binary_bytes} bytes total, "
                                        f"window peak={window_peak}, rms={rms:.1f}"
                                    )
                                    last_logged_at = binary_count
                                    window_peak = 0
                                    window_sum_sq = 0.0
                                    window_sample_count = 0
                            elif msg.type == WSMsgType.TEXT:
                                # Optional CHIRP control events from Bluejay.
                                # We trust AAI's audio-driven VAD for barge-in,
                                # so we just log these.
                                try:
                                    event = json.loads(msg.data)
                                    print(f"  Bluejay → {event.get('type')}: {event.get('data')}")
                                except Exception:
                                    pass
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break
                            else:
                                other_types[msg.type] = other_types.get(msg.type, 0) + 1
                        print(f"  Bluejay→AAI pump done: {binary_count} binary frames, {binary_bytes} bytes, other={other_types}")
                    finally:
                        # Bluejay hung up — wait briefly for any in-flight
                        # agent utterance to finish before closing AAI, so
                        # the final transcript event lands.
                        try:
                            await asyncio.wait_for(agent_idle.wait(), timeout=60)
                        except asyncio.TimeoutError:
                            pass
                        # session.end stops billing immediately. Just closing
                        # the socket keeps the session resumable (and billed)
                        # for another 30 seconds.
                        try:
                            await aai_ws.send_json({"type": "session.end"})
                            await asyncio.wait_for(session_ended.wait(), timeout=5)
                        except Exception:
                            pass
                        await aai_ws.close()

                async def aai_to_bluejay():
                    """Pump from AAI (JSON events) to Bluejay (binary + CHIRP text)."""
                    nonlocal session_id, current_utterance_id
                    downsample_state = None  # carried across audioop.ratecv calls
                    try:
                        async for msg in aai_ws:
                            if msg.type != WSMsgType.TEXT:
                                continue
                            event = json.loads(msg.data)
                            t = event.get("type")

                            if t == "session.ready":
                                session_id = event.get("session_id")
                                session_ready.set()
                                print(f"  Session ready: {session_id}")

                            elif t == "transcript.user":
                                text = event.get("text", "")
                                if text:
                                    transcript.append({
                                        "role": "user",
                                        "text": text,
                                        "ts": datetime.now(timezone.utc).isoformat(),
                                    })
                                    print(f"  User: {text}")

                            elif t == "transcript.agent":
                                text = event.get("text", "")
                                interrupted = event.get("interrupted", False)
                                if text:
                                    entry = {
                                        "role": "agent",
                                        "text": text,
                                        "interrupted": interrupted,
                                        "ts": datetime.now(timezone.utc).isoformat(),
                                    }
                                    # Dedup of double-emitted utterance.
                                    if (transcript
                                            and transcript[-1].get("role") == "agent"
                                            and transcript[-1].get("text") == text):
                                        transcript[-1] = entry
                                    else:
                                        transcript.append(entry)
                                    tag = " (interrupted)" if interrupted else ""
                                    print(f"  Agent: {text}{tag}")

                            elif t == "reply.audio":
                                agent_idle.clear()
                                # First audio chunk of a new agent turn —
                                # emit CHIRP speech.started with a fresh id.
                                if current_utterance_id is None:
                                    current_utterance_id = f"u_{uuid.uuid4().hex[:12]}"
                                    await safe_send_text(make_chirp(
                                        "speech.started",
                                        {"utterance_id": current_utterance_id},
                                    ))
                                audio_b64 = event.get("data", "")
                                if audio_b64:
                                    pcm24k = base64.b64decode(audio_b64)
                                    pcm16k, downsample_state = downsample_24_to_16(pcm24k, downsample_state)
                                    await safe_send_bytes(pcm16k)

                            elif t == "tool.call":
                                # Spawn the MCP call in the background so the
                                # audio pump stays responsive while it runs.
                                pending_tool_tasks.append(
                                    asyncio.create_task(run_tool(event))
                                )

                            elif t == "reply.done":
                                # End of an agent utterance. Tell Bluejay,
                                # and flush any pending tool results (or drop
                                # them on interrupt).
                                if current_utterance_id is not None:
                                    await safe_send_text(make_chirp(
                                        "speech.completed",
                                        {"utterance_id": current_utterance_id},
                                    ))
                                    current_utterance_id = None
                                agent_idle.set()
                                if event.get("status") == "interrupted":
                                    for task in pending_tool_tasks:
                                        task.cancel()
                                    pending_tool_tasks.clear()
                                elif pending_tool_tasks:
                                    results = await asyncio.gather(
                                        *pending_tool_tasks, return_exceptions=True
                                    )
                                    pending_tool_tasks.clear()
                                    for r in results:
                                        if isinstance(r, dict) and r.get("call_id"):
                                            await aai_ws.send_json({
                                                "type": "tool.result",
                                                "call_id": r["call_id"],
                                                "result": r["result"],
                                                "is_error": r.get("is_error", False),
                                            })

                            elif t in ("error", "session.error"):
                                msg_text = event.get("message", event.get("code", "Unknown error"))
                                print(f"  AAI error: {event}")
                                await safe_send_text(make_chirp(
                                    "session.error",
                                    {"code": "INTERNAL_ERROR", "message": msg_text},
                                ))

                            elif t == "input.speech.started":
                                print("  AAI detected user speech")

                            elif t == "input.speech.stopped":
                                print("  AAI detected end of user speech")

                            elif t == "session.ended":
                                audio_s = event.get("audio_duration_seconds")
                                print(
                                    f"  Session ended: "
                                    f"{event.get('session_duration_seconds')}s wall clock, "
                                    f"audio in: {f'{audio_s}s' if audio_s is not None else 'n/a'}"
                                )
                                session_ended.set()

                            elif t == "transcript.user.delta":
                                # Interim transcripts — useful to know AAI
                                # is actually receiving and decoding our audio.
                                interim = event.get("text", "")
                                if interim:
                                    print(f"  User (interim): {interim}")
                    finally:
                        if not bluejay_ws.closed:
                            await bluejay_ws.close()

                try:
                    await asyncio.gather(bluejay_to_aai(), aai_to_bluejay())
                finally:
                    for task in pending_tool_tasks:
                        task.cancel()
                    mcp_shutdown.set()
                    try:
                        await asyncio.wait_for(mcp_task, timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass

    except Exception as e:
        print(f"Session error: {e}")

    duration = (datetime.now(timezone.utc) - session_start).total_seconds()
    print(f"Bluejay disconnected (session: {duration:.0f}s, {len(transcript)} turns)")

    return bluejay_ws


# ---------------------------------------------------------------------------
# HTTP routing
# ---------------------------------------------------------------------------


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


async def root_handler(request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await bluejay_handler(request)
    return web.Response(text="Bluejay <-> AAI bridge — open a WebSocket to /voice", status=200)


async def voice_handler(request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await bluejay_handler(request)
    return web.Response(text="Open a WebSocket here.", status=400)


async def health(request):
    return web.json_response({"ok": True, "service": "bluejay-aai-bridge"})


async def main():
    if not ASSEMBLYAI_API_KEY:
        raise SystemExit("Missing ASSEMBLYAI_API_KEY env var.")

    print(f"Bluejay <-> AAI bridge — port {PORT}")
    print(f"Upstream: {AAI_WS_URL}")
    if AAI_AGENT_ID:
        print(f"Stored agent: {AAI_AGENT_ID}")
    print(f"CHIRP auth required: {bool(CHIRP_USER and CHIRP_PASS)}")

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", root_handler)
    app.router.add_get("/voice", voice_handler)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"Ready — ws://0.0.0.0:{PORT}/voice")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
