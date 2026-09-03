#!/usr/bin/env python3
"""Let Bluejay call your agent.

    AGENT_ID=<id> python bridge.py
    AGENT=<name>  python bridge.py     # a file you put in agents/

Bluejay opens a WebSocket to this server and speaks CHIRP: 16 kHz mono
pcm_s16le audio in binary frames plus optional JSON text events. For each one
the bridge opens a second WebSocket to the Voice Agent API (24 kHz mono
pcm_s16le, base64 inside JSON events) and translates between the two.

    Bluejay ──CHIRP──▶ this bridge ──Voice Agent API──▶ your agent
         16 kHz PCM + events       24 kHz PCM + JSON events

The session message contains only {agent_id}, so prompt, voice, tools and turn
detection are read from the published agent. Behaviour belongs in agents/, not
in here: this file is transport only.

  CHIRP:  https://docs.getbluejay.ai/simulation-integrations/websockets
  Events: https://www.assemblyai.com/docs/voice-agents/voice-agent-api/events-reference
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import time
import uuid
import warnings
from typing import Any, Optional

with warnings.catch_warnings():
    # audioop is deprecated in 3.12 and gone in 3.13, where `audioop-lts`
    # (requirements.txt) provides the same module.
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

import aiohttp
from aiohttp import WSCloseCode, WSMsgType, web

from lib import (AGENT_DIR, ApiError, aai, load_env, publish_agent, read_agent,
                 required, stored_agent_id)

load_env()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AAI_WS_URL = os.getenv("AAI_WS_URL", "wss://agents.assemblyai.com/v1/ws")
# Which agents/<name>.jsonc this bridge serves, when you keep the agent as a
# file. AGENT_ID in the environment takes precedence and needs no file.
AGENT = os.getenv("AGENT", "")
PORT = int(os.getenv("PORT", "8767"))

# Filled in at startup by resolve_agent().
AGENT_ID = ""

# HTTP Basic auth on the WebSocket upgrade, per CHIRP. If unset the server
# accepts any connection, which is fine locally and not fine deployed.
CHIRP_USER = os.getenv("CHIRP_USER", "")
CHIRP_PASS = os.getenv("CHIRP_PASS", "")

# Optional: report tool calls and the AssemblyAI session id back to Bluejay
# after each simulation (https://docs.getbluejay.ai/test/simulations/tool-calls).
BLUEJAY_API_KEY = os.getenv("BLUEJAY_API_KEY", "")
BLUEJAY_API_URL = os.getenv("BLUEJAY_API_URL", "https://api.getbluejay.ai")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Whether what was said appears in the logs. On by default: watching the
# conversation is how you follow a simulation, and the caller is a synthetic
# Digital Human rather than a real person. Turn it off on a shared or hosted
# bridge, where the lines land in someone else's log aggregator, or when the
# Digital Humans are seeded with realistic personal data.
LOG_TRANSCRIPTS = os.getenv("LOG_TRANSCRIPTS", "1").strip().lower() not in ("0", "false", "no", "off")

# Audio: Bluejay is 16 kHz, the Voice Agent API is 24 kHz; both mono pcm_s16le.
BLUEJAY_RATE = 16_000
AAI_RATE = 24_000
SAMPLE_WIDTH = 2  # int16
BLUEJAY_FRAME_MS = 20  # CHIRP's recommended frame size
BLUEJAY_FRAME_BYTES = BLUEJAY_RATE * SAMPLE_WIDTH * BLUEJAY_FRAME_MS // 1000  # 640

# Agent audio is sent to Bluejay at real-time pace, at most this far ahead of
# the clock. Keeping Bluejay's playback buffer short is what makes barge-in
# work: when the agent is interrupted we drop what hasn't been sent yet, and
# only this much can still be queued on Bluejay's side.
PLAYOUT_LEAD_S = 0.2

# The Voice Agent API discards input.audio sent before session.ready and drops
# audio streamed faster than real time, so audio that arrives before the
# session is up is held (newest wins) and flushed as one small burst.
PRE_READY_BUFFER_S = 1.0
PRE_READY_BUFFER_BYTES = int(AAI_RATE * SAMPLE_WIDTH * PRE_READY_BUFFER_S)

# How long to wait for session.ended after sending session.end.
SESSION_END_TIMEOUT_S = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")
log.setLevel(LOG_LEVEL)
for _noisy in ("aiohttp.access", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


class SessionLog:
    """Prefixes every line with a short per-connection tag so concurrent
    simulations can be told apart in the logs."""

    def __init__(self, tag: str):
        self.tag = tag

    def debug(self, msg: str, *args: Any) -> None:
        log.debug(f"[{self.tag}] {msg}", *args)

    def info(self, msg: str, *args: Any) -> None:
        log.info(f"[{self.tag}] {msg}", *args)

    def warning(self, msg: str, *args: Any) -> None:
        log.warning(f"[{self.tag}] {msg}", *args)

    def error(self, msg: str, *args: Any) -> None:
        log.error(f"[{self.tag}] {msg}", *args)

    def exception(self, msg: str, *args: Any) -> None:
        log.exception(f"[{self.tag}] {msg}", *args)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


class Resampler:
    """Streaming sample-rate conversion with stdlib audioop.ratecv.

    ratecv is stateful: the filter state is carried across calls, so 10-20 ms
    chunk boundaries don't produce artifacts. (A per-chunk polyphase resampler
    such as scipy's resample_poly re-settles its filter on every chunk, which
    the Voice Agent API's speech-to-text could not decode.)
    """

    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.state: Any = None

    def process(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        out, self.state = audioop.ratecv(pcm, SAMPLE_WIDTH, 1, self.src_rate, self.dst_rate, self.state)
        return out


class Framer:
    """Re-chunk a byte stream into fixed-size frames."""

    def __init__(self, frame_bytes: int):
        self.frame_bytes = frame_bytes
        self.buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self.buf += data
        frames = []
        while len(self.buf) >= self.frame_bytes:
            frames.append(bytes(self.buf[: self.frame_bytes]))
            del self.buf[: self.frame_bytes]
        return frames

    def flush(self) -> bytes:
        """Return whatever is left (even length, as CHIRP requires) and reset."""
        out = bytes(self.buf)
        self.buf.clear()
        if len(out) % SAMPLE_WIDTH:
            out = out[: -(len(out) % SAMPLE_WIDTH)]
        return out

    def reset(self) -> None:
        self.buf.clear()


# ---------------------------------------------------------------------------
# CHIRP helpers
# ---------------------------------------------------------------------------


# Anything a caller said, or a model wrote, is untrusted text on its way to a
# log line. Control characters are stripped so it cannot forge one, and the
# length is bounded so a long turn cannot flood the file.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def spoken(text: str, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", _CONTROL.sub(" ", text)).strip()
    return text if len(text) <= limit else text[:limit] + "..."


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


def authorized(request: web.Request) -> bool:
    expected = expected_basic_auth()
    if expected is None:
        return True
    got = request.headers.get("Authorization", "")
    return hmac.compare_digest(got.encode(), expected.encode())


# ---------------------------------------------------------------------------
# The bridge: one instance per Bluejay connection
# ---------------------------------------------------------------------------


class Bridge:
    def __init__(self, request: web.Request, bluejay_ws: web.WebSocketResponse, slog: SessionLog):
        self.bluejay_ws = bluejay_ws
        self.slog = slog
        self.simulation_result_id = request.headers.get("X-Simulation-Result-Id")
        self.started = time.monotonic()

        self.aai_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.session_id: Optional[str] = None
        self.voice: Optional[str] = None
        self.session_ready = asyncio.Event()
        # Tools with an http block, from the resolved config in session.ready.
        # AssemblyAI runs these itself and its tool.call is a notification, so
        # answering one would be wrong.
        self.server_tools: set[str] = set()
        self.session_ended = asyncio.Event()
        self.fatal_reported = False

        # Bluejay -> AAI
        self.upsampler = Resampler(BLUEJAY_RATE, AAI_RATE)
        self.pre_ready = bytearray()
        self.frames_in = 0
        self.bytes_in = 0

        # AAI -> Bluejay
        self.downsampler = Resampler(AAI_RATE, BLUEJAY_RATE)
        self.framer = Framer(BLUEJAY_FRAME_BYTES)
        self.play_q: asyncio.Queue = asyncio.Queue()
        self.play_gen = 0  # bumped on interruption; stale queue items are dropped
        self.utterance_id: Optional[str] = None
        self.marks_sent: dict[str, float] = {}

        self.tool_log: list[dict[str, Any]] = []

        self.user_turns = 0
        self.agent_turns = 0

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        headers = {"Authorization": f"Bearer {os.environ['ASSEMBLYAI_API_KEY']}"}
        async with aiohttp.ClientSession() as http:
            try:
                self.aai_ws = await http.ws_connect(AAI_WS_URL, headers=headers)
            except Exception as e:  # noqa: BLE001
                self.slog.error("Could not connect to %s: %s", AAI_WS_URL, e)
                await self.fail_bluejay(f"Could not reach AssemblyAI: {e}")
                return

            async with self.aai_ws:
                # session.update goes first, so the greeting fires as early as possible.
                await self.aai_ws.send_json(self.first_session_update())

                playout = asyncio.create_task(self.playout())
                try:
                    await asyncio.gather(self.pump_bluejay_to_aai(), self.pump_aai_to_bluejay())
                finally:
                    playout.cancel()

        await self.report_to_bluejay()

    def first_session_update(self) -> dict:
        """agent_id is mutually exclusive with the inline fields, so this is the
        whole message. https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-configuration"""
        return {"type": "session.update", "session": {"agent_id": AGENT_ID}}

    # -- Bluejay -> AAI -------------------------------------------------------

    async def pump_bluejay_to_aai(self) -> None:
        """Binary frames become input.audio; CHIRP text events are logged."""
        level_window = bytearray()  # ~2 s of raw frames for DEBUG level stats
        try:
            async for msg in self.bluejay_ws:
                if msg.type == WSMsgType.BINARY:
                    data: bytes = msg.data
                    if len(data) % SAMPLE_WIDTH:  # CHIRP requires even length; tolerate anyway
                        data = data[:-1]
                    if not data:
                        continue
                    self.frames_in += 1
                    self.bytes_in += len(data)
                    if self.frames_in == 1:
                        self.slog.info("First audio from Bluejay (%d-byte frames)", len(data))
                    if log.isEnabledFor(logging.DEBUG):
                        level_window += data
                        if len(level_window) >= BLUEJAY_RATE * SAMPLE_WIDTH * 2:
                            self.slog.debug(
                                "Bluejay audio: %d frames so far, last 2 s peak=%d rms=%d",
                                self.frames_in, audioop.max(level_window, SAMPLE_WIDTH),
                                audioop.rms(level_window, SAMPLE_WIDTH),
                            )
                            level_window.clear()

                    pcm24 = self.upsampler.process(data)
                    if self.session_ready.is_set():
                        await self.send_audio_to_aai(pcm24)
                    else:
                        self.pre_ready += pcm24
                        excess = len(self.pre_ready) - PRE_READY_BUFFER_BYTES
                        if excess > 0:
                            del self.pre_ready[:excess]

                elif msg.type == WSMsgType.TEXT:
                    self.on_bluejay_event(msg.data)

                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            self.slog.info(
                "Bluejay connection closed after %d frames (%.1f s of audio)",
                self.frames_in, self.bytes_in / (BLUEJAY_RATE * SAMPLE_WIDTH),
            )
            await self.end_aai_session()

    def on_bluejay_event(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self.slog.warning("Bluejay sent non-JSON text frame: %.80s", raw)
            return
        t = event.get("type")
        data = event.get("data") or {}
        if t == "speech.started":
            # Bluejay's VAD says the Digital Human started talking. The agent's
            # own turn detection works from the audio, so nothing to forward.
            self.slog.debug("Digital Human speaking (utterance %s)", data.get("utterance_id"))
        elif t == "speech.completed":
            self.slog.debug("Digital Human finished (utterance %s)", data.get("utterance_id"))
        elif t == "mark":
            # Echo of a mark we sent after an agent utterance: Bluejay has now
            # played all of that utterance's audio to the Digital Human.
            name = data.get("name")
            sent_at = self.marks_sent.pop(name, None)
            if sent_at is not None:
                self.slog.debug("Utterance %s finished playing at Bluejay (%.0f ms after last frame)",
                                name, (time.monotonic() - sent_at) * 1000)
        elif t == "session.error":
            self.slog.warning("Bluejay reported %s: %s", data.get("code"), data.get("message"))
        else:
            self.slog.debug("Bluejay sent %s: %s", t, data)

    async def send_audio_to_aai(self, pcm24: bytes) -> None:
        if not pcm24 or self.aai_ws is None or self.aai_ws.closed:
            return
        try:
            await self.aai_ws.send_json({"type": "input.audio", "audio": base64.b64encode(pcm24).decode()})
        except Exception as e:  # noqa: BLE001
            self.slog.debug("Dropping audio, upstream closed: %s", e)

    async def end_aai_session(self) -> None:
        """session.end stops billing immediately. Closing the socket without it
        leaves the session resumable, and billed, for another 30 seconds."""
        if self.aai_ws is None or self.aai_ws.closed:
            return
        try:
            await self.aai_ws.send_json({"type": "session.end"})
            await asyncio.wait_for(self.session_ended.wait(), SESSION_END_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.slog.warning("No session.ended within %.0fs of session.end", SESSION_END_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            self.slog.debug("session.end failed: %s", e)
        finally:
            await self.aai_ws.close()

    # -- AAI -> Bluejay -------------------------------------------------------

    async def pump_aai_to_bluejay(self) -> None:
        try:
            async for msg in self.aai_ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    continue
                try:
                    event = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self.on_aai_event(event)
        finally:
            await self.close_bluejay()

    async def on_aai_event(self, event: dict) -> None:
        t = event.get("type")

        if t == "session.ready":
            self.session_id = event.get("session_id")
            self.session_ready.set()
            # The resolved config: what the stored agent actually became.
            config = event.get("config") or {}
            self.voice = (config.get("output") or {}).get("voice")
            self.server_tools = {tool["name"] for tool in config.get("tools") or []
                                 if tool.get("http")}
            expires = event.get("expires_at")
            self.slog.info("AssemblyAI session %s ready%s", self.session_id,
                           f" (expires {time.strftime('%H:%M:%S', time.gmtime(expires))} UTC)" if expires else "")
            if self.pre_ready:
                self.slog.debug("Flushing %.2f s of pre-ready audio",
                                len(self.pre_ready) / (AAI_RATE * SAMPLE_WIDTH))
                await self.send_audio_to_aai(bytes(self.pre_ready))
                self.pre_ready.clear()

        elif t == "session.updated":
            self.slog.debug("session.updated")

        elif t == "input.speech.started":
            self.slog.debug("Agent hears the Digital Human speaking")
        elif t == "input.speech.stopped":
            self.slog.debug("Agent hears the Digital Human stop")
        elif t == "transcript.user.delta":
            if LOG_TRANSCRIPTS:
                self.slog.debug("User (partial): %s", spoken(event.get("text", "")))

        elif t == "transcript.user":
            self.user_turns += 1
            self.log_turn("User", event.get("text", ""))

        elif t == "transcript.agent.delta":
            pass  # word-level captions; not needed for a bridge

        elif t == "transcript.agent":
            self.agent_turns += 1
            self.log_turn("Agent", event.get("text", ""),
                          " (interrupted)" if event.get("interrupted") else "")

        elif t == "reply.started":
            self.slog.debug("reply.started %s", event.get("reply_id"))

        elif t == "reply.audio":
            await self.on_reply_audio(event.get("data", ""))

        elif t == "reply.done":
            await self.on_reply_done(event.get("status", "completed"))

        elif t == "tool.call":
            await self.on_tool_call(event)

        elif t in ("session.error", "error"):
            await self.on_aai_error(event)

        elif t == "session.ended":
            self.session_ended.set()
            audio_s = event.get("audio_duration_seconds")
            self.slog.info("AssemblyAI session ended: %ss wall clock, audio in: %s",
                           event.get("session_duration_seconds"),
                           f"{audio_s}s" if audio_s is not None else "n/a")

        else:
            self.slog.debug("Unhandled event %s", t)

    def log_turn(self, who: str, text: str, suffix: str = "") -> None:
        """What was said, or only that something was, per LOG_TRANSCRIPTS."""
        if LOG_TRANSCRIPTS:
            self.slog.info("%s: %s%s", who, spoken(text), suffix)
        else:
            self.slog.info("%s: %d characters%s", who, len(text), suffix)

    async def on_reply_audio(self, audio_b64: str) -> None:
        if not audio_b64:
            return
        pcm16 = self.downsampler.process(base64.b64decode(audio_b64))
        if self.utterance_id is None:
            # First audio of a new agent turn. Announcing it before the audio
            # also makes Bluejay interrupt the Digital Human if it is mid-utterance.
            self.utterance_id = f"u_{uuid.uuid4().hex[:12]}"
            self.framer.reset()
            await self.send_chirp("speech.started", {"utterance_id": self.utterance_id})
        for frame in self.framer.push(pcm16):
            self.play_q.put_nowait((self.play_gen, "audio", frame))

    async def on_reply_done(self, status: str) -> None:
        if status == "interrupted":
            # The Digital Human barged in. Drop everything not yet sent so the
            # agent goes quiet at Bluejay within PLAYOUT_LEAD_S, and discard
            # any tool results that were waiting for this reply to finish.
            self.play_gen += 1
            while not self.play_q.empty():
                self.play_q.get_nowait()
            self.framer.reset()
            if self.utterance_id is not None:
                await self.send_chirp("speech.completed", {"utterance_id": self.utterance_id})
                self.utterance_id = None
            return

        if self.utterance_id is not None:
            tail = self.framer.flush()
            if tail:
                self.play_q.put_nowait((self.play_gen, "audio", tail))
            # speech.completed is sent by playout() once the last frame has gone out.
            self.play_q.put_nowait((self.play_gen, "end", self.utterance_id))
            self.utterance_id = None

    async def playout(self) -> None:
        """Send agent audio to Bluejay at real-time pace (see PLAYOUT_LEAD_S)."""
        loop = asyncio.get_running_loop()
        next_at: Optional[float] = None
        while True:
            gen, kind, payload = await self.play_q.get()
            if gen != self.play_gen:
                continue  # belongs to an interrupted utterance
            if kind == "end":
                await self.send_chirp("speech.completed", {"utterance_id": payload})
                # Ask Bluejay to tell us when this utterance has actually played out.
                await self.send_chirp("mark", {"name": payload})
                self.marks_sent[payload] = time.monotonic()
                next_at = None
                continue
            now = loop.time()
            if next_at is None or now > next_at + 0.5:
                next_at = now  # start of an utterance, or we fell behind: resync
            if next_at - now > PLAYOUT_LEAD_S:
                await asyncio.sleep(next_at - now - PLAYOUT_LEAD_S)
                if gen != self.play_gen:
                    continue  # interrupted while we slept
            await self.send_bytes(payload)
            next_at += len(payload) / (BLUEJAY_RATE * SAMPLE_WIDTH)

    # -- tools ------------------------------------------------------------------

    async def on_tool_call(self, event: dict) -> None:
        """Tools with an http block are run by AssemblyAI, and the tool.call is
        only a notification, so it is logged and left alone. Anything else has
        to be answered by whoever holds the session. The bridge is transport
        only, so it answers with an error rather than letting the call stall
        until the tool's timeout_seconds.

        https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/overview
        """
        name = event.get("name", "")
        args = event.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        self.tool_log.append({
            "name": name,
            "parameters": args,
            "start_offset_ms": int((time.monotonic() - self.started) * 1000),
        })

        if name in self.server_tools:
            self.slog.info("Tool call: %s(%s)", name, self.log_args(args))
            return

        self.slog.warning("Tool %s has no http block, so nothing here can answer it. "
                          "See agents/README.md", name)  # name only, arguments may carry PII
        if self.aai_ws is not None and not self.aai_ws.closed:
            await self.aai_ws.send_json({
                "type": "tool.result",
                "call_id": event.get("call_id", ""),
                "result": json.dumps({
                    "error": f"{name} is not available during this simulation. "
                             "Tell the caller you cannot do that right now.",
                }),
                "is_error": True,
            })

    @staticmethod
    def log_args(args: dict) -> str:
        """Tool arguments are lifted from what the caller said, so they are
        conversation content too. Without transcripts, log the names only."""
        if LOG_TRANSCRIPTS:
            return spoken(json.dumps(args), 200)
        return ", ".join(f"{key}=..." for key in args)

    # -- errors and teardown ------------------------------------------------------

    async def on_aai_error(self, event: dict) -> None:
        code = event.get("code", "unknown")
        message = event.get("message", "")
        # Connection-level errors, and anything before the session is up, are
        # fatal: the config is fixed, so there is nothing to retry with.
        fatal = event.get("type") == "error" or not self.session_ready.is_set()
        if fatal:
            self.slog.error("AssemblyAI %s: %s", code, message)
            await self.fail_bluejay(f"AssemblyAI {code}: {message}")
        else:
            self.slog.warning("AssemblyAI %s: %s", code, message)

    async def fail_bluejay(self, reason: str) -> None:
        """Tell Bluejay the call failed (surfaces in its test-result metadata) and hang up."""
        if self.bluejay_ws.closed or self.fatal_reported:
            return
        self.fatal_reported = True
        await self.send_chirp("session.error", {"code": "INTERNAL_ERROR", "message": reason})
        await self.bluejay_ws.close(code=WSCloseCode.INTERNAL_ERROR, message=reason.encode()[:120])

    async def close_bluejay(self) -> None:
        if self.bluejay_ws.closed:
            return
        if self.session_ended.is_set() or self.fatal_reported:
            await self.bluejay_ws.close(code=WSCloseCode.OK, message=b"call ended")
        else:
            await self.fail_bluejay("AssemblyAI connection closed unexpectedly")

    async def send_chirp(self, event_type: str, data: dict) -> None:
        if self.bluejay_ws.closed:
            return
        try:
            await self.bluejay_ws.send_str(make_chirp(event_type, data))
        except Exception:  # noqa: BLE001
            pass

    async def send_bytes(self, payload: bytes) -> None:
        if self.bluejay_ws.closed or not payload:
            return
        try:
            await self.bluejay_ws.send_bytes(payload)
        except Exception:  # noqa: BLE001
            pass

    async def report_to_bluejay(self) -> None:
        """Enrich Bluejay's simulation result with the tool calls the agent made
        and the AssemblyAI session id (so the recording is one lookup away)."""
        if not (BLUEJAY_API_KEY and self.simulation_result_id):
            return
        metadata = {
            "assemblyai_session_id": self.session_id,
            "assemblyai_agent_id": AGENT_ID,
            "voice": self.voice,
        }
        payload: dict[str, Any] = {
            "simulation_result_id": self.simulation_result_id,
            "metadata": {k: v for k, v in metadata.items() if v},
        }
        if self.tool_log:  # only client-executed tools reach us; http tools run server-side
            payload["tool_calls"] = self.tool_log
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{BLUEJAY_API_URL}/v1/update-simulation-result",
                    json=payload,
                    headers={"X-API-Key": BLUEJAY_API_KEY},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        self.slog.info("Reported %d tool call(s) to Bluejay", len(self.tool_log))
                    else:
                        self.slog.warning("Bluejay update-simulation-result returned %s: %.200s",
                                          resp.status, await resp.text())
        except Exception as e:  # noqa: BLE001
            self.slog.warning("Could not report to Bluejay: %s", e)


# ---------------------------------------------------------------------------
# HTTP routing
# ---------------------------------------------------------------------------


async def bluejay_handler(request: web.Request) -> web.StreamResponse:
    if not authorized(request):
        log.warning("CHIRP auth rejected from %s", request.remote)
        return web.Response(status=401, text="Unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="bluejay-aai-bridge"'})

    bluejay_ws = web.WebSocketResponse()
    await bluejay_ws.prepare(request)

    slog = SessionLog(uuid.uuid4().hex[:6])
    bridge = Bridge(request, bluejay_ws, slog)
    slog.info("Bluejay connected%s",
              f" (simulation result {bridge.simulation_result_id})" if bridge.simulation_result_id else "")
    try:
        await bridge.run()
    except Exception:  # noqa: BLE001
        slog.exception("Session crashed")
        await bridge.fail_bluejay("bridge error")
    finally:
        hint = f"; recording: python sessions.py {bridge.session_id}" if bridge.session_id else ""
        slog.info("Session closed after %.0fs: %d user turns, %d agent turns%s",
                  time.monotonic() - bridge.started, bridge.user_turns, bridge.agent_turns, hint)
    return bluejay_ws


async def root_handler(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await bluejay_handler(request)
    return web.Response(text="Bluejay <-> AssemblyAI Voice Agent bridge. Open a WebSocket to /voice.")


async def voice_handler(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await bluejay_handler(request)
    return web.Response(status=400, text="Open a WebSocket here.")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "bluejay-aai-bridge", "agent_id": AGENT_ID})


NO_AGENT = """Nothing to test yet. Point the bridge at your agent, either way:

  1. An agent you already have. Put its id in .env:

       AGENT_ID=7ad24396-b822-4dca-871a-be9cc4781cf9

  2. Or keep it in the repo as a file you can edit and publish:

       python import_agent.py <agent-id>     # writes agents/<its-name>.jsonc
       AGENT=<name> python bridge.py

The id is in the agent's URL in the AssemblyAI dashboard."""


def resolve_agent() -> dict:
    """An id in the environment means the agent is managed elsewhere, so it is
    used as it is. Otherwise publish agents/<AGENT>.jsonc and use that. There
    are no agents in this repo to fall back on: the one under test is yours."""
    known = stored_agent_id(AGENT)
    if known:
        try:
            agent = aai(f"/agents/{known}")
        except ApiError as err:
            raise SystemExit(f"Could not load agent {known}: {err}")
        return {"id": known, "name": agent.get("name") or "Your agent"}
    if not AGENT:
        raise SystemExit(NO_AGENT)
    if not (AGENT_DIR / f"{AGENT}.jsonc").exists():
        raise SystemExit(f"No agents/{AGENT}.jsonc.\n\n{NO_AGENT}")
    agent = read_agent(AGENT)
    try:
        result = publish_agent(agent, name=AGENT, reuse_by_name=True)
    except ApiError as err:
        raise SystemExit(f"Could not publish agents/{AGENT}.jsonc: {err}")
    log.info('%s "%s" from agents/%s.jsonc',
             "Created" if result["created"] else "Updated", agent["name"], AGENT)
    return {"id": result["id"], "name": agent["name"]}


async def main() -> None:
    global AGENT_ID
    required("ASSEMBLYAI_API_KEY",
             "cp .env.example .env and add your key from "
             "https://www.assemblyai.com/dashboard/api-keys")
    agent = resolve_agent()
    AGENT_ID = agent["id"]
    log.info('Agent: "%s" (%s)', agent["name"], AGENT_ID)
    log.info("Upstream: %s", AAI_WS_URL)
    if not (CHIRP_USER and CHIRP_PASS):
        log.warning("No CHIRP_USER / CHIRP_PASS set: anyone who can reach this port can "
                    "start a call on your key. Fine locally, set both before hosting.")

    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/voice", voice_handler)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Ready: ws://0.0.0.0:%d/voice", PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
