#!/usr/bin/env python3
"""Place a call to the bridge yourself, without Bluejay.

    python call.py
    python call.py --wav question.wav --seconds 30
    python call.py wss://your-host/voice --user bluejay --pass s3cret

The pre-flight check: it speaks CHIRP the way Bluejay does, so if a call
through here sounds right, a real simulation will too. Worth running against a
deployment before pointing a suite of simulations at it, since every one of
those costs money on both accounts. Follows
https://docs.getbluejay.ai/simulation-integrations/websockets:
  * Basic-auth WebSocket upgrade with an X-Simulation-Result-Id header.
  * After --delay seconds of silence, which lets the greeting play, streams
    --wav as 16 kHz mono pcm_s16le in 20 ms binary frames at real-time pace,
    wrapped in speech.started / speech.completed, then silence until --seconds.
  * Prints every text event, echoes marks the way Bluejay does, and records the
    agent's audio to --out as a 16 kHz mono WAV.

With no --wav it just listens, which is enough to hear the greeting. Any
16-bit PCM WAV works as input; other rates and stereo are converted. On macOS
the built-in speech synthesiser makes one:

    say -o q.aiff "Hi, I have a question about my order" \
      && afconvert -f WAVE -d LEI16@16000 -c 1 q.aiff question.wav

Interrupting the agent is what --delay is for: set it low enough that the
question starts while the greeting is still playing.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import uuid
import warnings
import wave

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)  # audioop: deprecated 3.12, audioop-lts on 3.13+
    import audioop

import aiohttp

from lib import load_env

RATE = 16_000
WIDTH = 2
FRAME_MS = 20
FRAME_BYTES = RATE * WIDTH * FRAME_MS // 1000  # 640
SILENCE = b"\x00" * FRAME_BYTES


def chirp(event_type: str, data: dict) -> str:
    return json.dumps({"type": event_type, "id": str(uuid.uuid4()),
                       "ts_ms": int(time.time() * 1000), "data": data})


def load_wav_as_16k_mono(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != WIDTH:
            sys.exit(f"{path}: need 16-bit PCM, got {8 * w.getsampwidth()}-bit")
        pcm = w.readframes(w.getnframes())
        if w.getnchannels() == 2:
            pcm = audioop.tomono(pcm, WIDTH, 0.5, 0.5)
        elif w.getnchannels() != 1:
            sys.exit(f"{path}: need mono or stereo, got {w.getnchannels()} channels")
        if w.getframerate() != RATE:
            pcm, _ = audioop.ratecv(pcm, WIDTH, 1, w.getframerate(), RATE, None)
    return pcm


async def run(args: argparse.Namespace) -> None:
    headers = {"X-Simulation-Result-Id": args.simulation_id}
    if args.user and args.password:
        token = base64.b64encode(f"{args.user}:{args.password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    speech = load_wav_as_16k_mono(args.wav) if args.wav else b""
    received = bytearray()
    events: list[str] = []
    t0 = time.monotonic()

    def stamp() -> str:
        return f"[{time.monotonic() - t0:6.2f}s]"

    async with aiohttp.ClientSession() as http:
        try:
            ws = await http.ws_connect(args.url, headers=headers)
        except aiohttp.WSServerHandshakeError as e:
            sys.exit(f"Handshake failed: HTTP {e.status} {e.message}")
        print(f"{stamp()} connected to {args.url}")

        async def sender() -> None:
            loop = asyncio.get_running_loop()
            next_at = loop.time()
            frames = [speech[i:i + FRAME_BYTES] for i in range(0, len(speech), FRAME_BYTES)]
            speak_from = int(args.delay * 1000 / FRAME_MS)
            utterance_id = f"dh_{uuid.uuid4().hex[:8]}"
            i = 0
            while time.monotonic() - t0 < args.seconds and not ws.closed:
                if i == speak_from and frames:
                    await ws.send_str(chirp("speech.started", {"utterance_id": utterance_id}))
                    print(f"{stamp()} -> speech.started (Digital Human, {len(speech) / (RATE * WIDTH):.1f}s of audio)")
                idx = i - speak_from
                if frames and 0 <= idx < len(frames):
                    frame = frames[idx]
                    if len(frame) % WIDTH:
                        frame = frame[:-1]
                    await ws.send_bytes(frame)
                    if idx == len(frames) - 1:
                        await ws.send_str(chirp("speech.completed", {"utterance_id": utterance_id}))
                        print(f"{stamp()} -> speech.completed")
                else:
                    await ws.send_bytes(SILENCE)
                i += 1
                next_at += FRAME_MS / 1000
                await asyncio.sleep(max(0.0, next_at - loop.time()))
            if not ws.closed:
                print(f"{stamp()} hanging up (close 1000)")
                await ws.close(code=aiohttp.WSCloseCode.OK, message=b"call ended")

        async def receiver() -> None:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    received.extend(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    event = json.loads(msg.data)
                    t, data = event.get("type"), event.get("data") or {}
                    events.append(t)
                    print(f"{stamp()} <- {t} {json.dumps(data)}")
                    if t == "mark":  # Bluejay echoes marks once the audio before them has played
                        await ws.send_str(chirp("mark", {"name": data.get("name")}))
            print(f"{stamp()} server closed: code={ws.close_code}")

        send_task = asyncio.create_task(sender())
        await receiver()
        send_task.cancel()

    if received:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with wave.open(args.out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(WIDTH)
            w.setframerate(RATE)
            w.writeframes(bytes(received))
    print(f"\nagent audio: {len(received) / (RATE * WIDTH):.1f}s -> {args.out if received else '(none received)'}")
    print(f"text events: {', '.join(events) or '(none)'}")


def main() -> None:
    load_env()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", nargs="?", default="ws://localhost:8767/voice")
    p.add_argument("--user", default=os.getenv("CHIRP_USER", ""))
    p.add_argument("--pass", dest="password", default=os.getenv("CHIRP_PASS", ""))
    p.add_argument("--wav", help="16-bit PCM WAV to play as the Digital Human's speech")
    p.add_argument("--delay", type=float, default=7.0, help="seconds of silence before speaking (default 7)")
    p.add_argument("--seconds", type=float, default=20.0, help="total call length (default 20)")
    p.add_argument("--out", default="out/agent.wav", help="where to write the agent's audio")
    p.add_argument("--simulation-id", default=f"local-{uuid.uuid4().hex[:8]}")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
