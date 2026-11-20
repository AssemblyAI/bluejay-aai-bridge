"""Reply boundaries must not grant another playout lead allowance."""

import asyncio
import base64
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge  # noqa: E402

FRAME_S = bridge.BLUEJAY_FRAME_BYTES / (bridge.BLUEJAY_RATE * bridge.SAMPLE_WIDTH)
# One frame of slack: a frame is sent while the lead is still at most PLAYOUT_LEAD_S.
MAX_LEAD_S = bridge.PLAYOUT_LEAD_S + FRAME_S + 1e-9


class PlayoutFinished(BaseException):
    """send_chirp swallows Exception, so stopping playout() needs a BaseException."""


def reply_audio(seconds: float, sample: int = 0) -> str:
    n = round(seconds * bridge.AAI_RATE)
    return base64.b64encode(struct.pack(f"<{n}h", *([sample] * n))).decode()


class FakeBluejay:
    """Models Bluejay's playback buffer: audio plays out in real time once sent."""

    def __init__(self, clock):
        self.clock = clock
        self.closed = False
        self.frames = []
        self.events = []
        self.played_until = clock.now
        self.max_lead = 0.0
        self.on_mark = None

    async def send_bytes(self, data):
        now = self.clock.now
        self.frames.append((now, data))
        self.played_until = max(self.played_until, now) + len(data) / (bridge.BLUEJAY_RATE * bridge.SAMPLE_WIDTH)
        self.max_lead = max(self.max_lead, self.played_until - now)

    async def send_str(self, raw):
        event = json.loads(raw)
        self.events.append(event["type"])
        if event["type"] == "mark" and self.on_mark:
            self.on_mark(self.events.count("mark"))


class Clock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now


class PlayoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.ws = FakeBluejay(self.clock)
        self.connection = bridge.Bridge(mock.Mock(headers={}), self.ws, mock.Mock())

    async def run_playout(self, sleep=None):
        async def advance(delay):
            self.assertGreaterEqual(delay, 0)
            self.clock.now += delay

        with mock.patch.object(bridge.asyncio, "get_running_loop", return_value=self.clock), \
                mock.patch.object(bridge.asyncio, "sleep", side_effect=sleep or advance):
            with self.assertRaises(PlayoutFinished):
                await asyncio.wait_for(self.connection.playout(), 5)

    async def measure(self, durations, idle=0.0):
        def on_mark(count):
            if count == len(durations):
                raise PlayoutFinished
            self.clock.now += idle

        self.ws.on_mark = on_mark
        for duration in durations:
            await self.connection.on_reply_audio(reply_audio(duration))
            await self.connection.on_reply_done("completed")
        await self.run_playout()
        self.assertEqual(self.ws.events.count("speech.completed"), len(durations))
        self.assertEqual(self.ws.events.count("mark"), len(durations))
        self.assertTrue(all(len(f) <= bridge.BLUEJAY_FRAME_BYTES and len(f) % 2 == 0
                            for _, f in self.ws.frames))
        self.assertLessEqual(self.ws.max_lead, MAX_LEAD_S)

    async def test_queued_short_replies_share_one_lead_allowance(self):
        await self.measure([0.2] * 10)

    async def test_queued_long_replies_with_partial_tails_share_clock(self):
        await self.measure([1.007, 2.013, 0.103])

    async def test_short_gap_between_replies_does_not_burst(self):
        await self.measure([0.4, 0.4], idle=0.45)

    async def test_idle_resynchronizes_without_catching_up(self):
        await self.measure([0.1, 0.1], idle=2.0)
        self.assertEqual(self.ws.frames[5][0], 102.0)

    async def test_interruption_discards_unsent_audio_and_keeps_lead_bound(self):
        stale_sent = []

        async def sleep(delay):
            self.assertGreaterEqual(delay, 0)
            if not stale_sent:
                stale_sent.append(len(self.ws.frames))
                await self.connection.on_reply_done("interrupted")
                await self.connection.on_reply_audio(reply_audio(0.1, 1000))
                await self.connection.on_reply_done("completed")
            self.clock.now += delay

        def on_mark(count):
            raise PlayoutFinished

        self.ws.on_mark = on_mark
        await self.connection.on_reply_audio(reply_audio(1.0))
        await self.run_playout(sleep)
        silent = [f for _, f in self.ws.frames if not any(f)]
        self.assertEqual(len(silent), stale_sent[0])
        self.assertGreater(len(self.ws.frames), stale_sent[0])
        self.assertTrue(self.connection.play_q.empty())
        self.assertLessEqual(self.ws.max_lead, MAX_LEAD_S)


if __name__ == "__main__":
    unittest.main()
