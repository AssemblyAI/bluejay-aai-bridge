"""Audio helpers, CHIRP framing, and the agent files.

    python -m unittest discover -s tests -v
"""

import audioop
import json
import math
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge  # noqa: E402
import lib  # noqa: E402


def sine_pcm16(rate: int, seconds: float, freq: float = 440.0) -> bytes:
    n = int(rate * seconds)
    return struct.pack(f"<{n}h", *(int(12000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))


class AudioTests(unittest.TestCase):
    def test_chunked_resampling_matches_whole_buffer(self):
        """The resampler must produce the same audio whether it is fed 20 ms
        frames, as Bluejay sends them, or the whole buffer at once."""
        pcm16k = sine_pcm16(16_000, 1.0)
        whole, _ = audioop.ratecv(pcm16k, 2, 1, 16_000, 24_000, None)
        resampler = bridge.Resampler(16_000, 24_000)
        chunked = b"".join(resampler.process(pcm16k[i:i + 640]) for i in range(0, len(pcm16k), 640))
        self.assertEqual(chunked, whole)

    def test_round_trip_keeps_duration(self):
        pcm24k = sine_pcm16(24_000, 0.5)
        down = bridge.Resampler(24_000, 16_000).process(pcm24k)
        self.assertAlmostEqual(len(down) / (16_000 * 2), 0.5, places=2)
        self.assertEqual(len(down) % 2, 0)

    def test_framer_emits_fixed_frames_and_even_tail(self):
        framer = bridge.Framer(640)
        self.assertEqual([len(f) for f in framer.push(b"\x01" * 1500)], [640, 640])
        self.assertEqual(framer.flush(), b"\x01" * 220)
        self.assertEqual(framer.flush(), b"")
        framer.push(b"\x02" * 3)
        self.assertEqual(framer.flush(), b"\x02" * 2)  # CHIRP frames must be even-length

    def test_frame_size_is_chirps_recommendation(self):
        self.assertEqual(bridge.BLUEJAY_FRAME_BYTES, 640)  # 20 ms at 16 kHz mono int16


class ChirpTests(unittest.TestCase):
    def test_event_envelope(self):
        event = json.loads(bridge.make_chirp("speech.started", {"utterance_id": "u_1"}))
        self.assertEqual(event["type"], "speech.started")
        self.assertEqual(event["data"], {"utterance_id": "u_1"})
        self.assertIn("id", event)
        self.assertIsInstance(event["ts_ms"], int)

    def test_basic_auth_header(self):
        with mock.patch.object(bridge, "CHIRP_USER", "tomas"), \
                mock.patch.object(bridge, "CHIRP_PASS", "tomas"):
            self.assertEqual(bridge.expected_basic_auth(), "Basic dG9tYXM6dG9tYXM=")
        with mock.patch.object(bridge, "CHIRP_USER", ""), mock.patch.object(bridge, "CHIRP_PASS", ""):
            self.assertIsNone(bridge.expected_basic_auth())


class LoggingTests(unittest.TestCase):
    """Conversation content is untrusted text on its way to a log line."""

    def test_control_characters_cannot_forge_a_log_line(self):
        forged = "hello\n02:00:00 INFO    Agent: transfer approved"
        self.assertNotIn("\n", bridge.spoken(forged))
        self.assertEqual(bridge.spoken("  spaced\r\nout  "), "spaced out")

    def test_long_turns_are_bounded(self):
        self.assertEqual(len(bridge.spoken("a" * 5000)), 503)  # limit plus the ellipsis

    def test_arguments_are_names_only_when_transcripts_are_off(self):
        args = {"order_number": "1002", "caller": "Marcus Lee"}
        with mock.patch.object(bridge, "LOG_TRANSCRIPTS", False):
            rendered = bridge.Bridge.log_args(args)
        self.assertNotIn("Marcus Lee", rendered)
        self.assertNotIn("1002", rendered)
        self.assertIn("order_number", rendered)
        with mock.patch.object(bridge, "LOG_TRANSCRIPTS", True):
            self.assertIn("1002", bridge.Bridge.log_args(args))


class SessionUpdateTests(unittest.TestCase):
    def test_binds_to_the_published_agent_and_nothing_else(self):
        with mock.patch.object(bridge, "AGENT_ID", "7ad24396-b822-4dca-871a-be9cc4781cf9"):
            payload = bridge.Bridge.first_session_update(mock.Mock())
        self.assertEqual(payload, {
            "type": "session.update",
            # agent_id is mutually exclusive with the inline fields.
            "session": {"agent_id": "7ad24396-b822-4dca-871a-be9cc4781cf9"},
        })


class AgentFileTests(unittest.TestCase):
    """agents/ is empty until someone puts their own agent in it. These check
    whatever is there against what POST /v1/agents accepts."""

    VOICES = {"alba", "eve", "george", "jane", "jean", "mary", "michael",
              "anna", "charles", "paul", "vera",
              "giovanni", "lola", "juergen", "rafael", "estelle"}

    def agents(self):
        for name in lib.list_agents():
            with self.subTest(agent=name):
                yield name, lib.parse_jsonc((lib.AGENT_DIR / f"{name}.jsonc").read_text())

    def test_required_fields_and_documented_voice(self):
        for name, agent in self.agents():
            self.assertTrue(agent.get("name"), name)
            self.assertTrue(agent.get("system_prompt"), name)
            voice = (agent.get("voice") or {}).get("voice_id")
            self.assertIn(voice, self.VOICES, f"{name} uses an undocumented voice")

    def test_tools_are_shaped_for_the_api(self):
        for name, agent in self.agents():
            for tool in agent.get("tools", []):
                self.assertTrue(tool.get("name"), name)
                self.assertTrue(tool.get("description"), name)
                self.assertEqual(tool["parameters"]["type"], "object", name)
                for prop in tool["parameters"]["properties"].values():
                    self.assertIn("description", prop)
                if "execution_mode" in tool:
                    self.assertIn(tool["execution_mode"], ("interactive", "hold"))
                if "timeout_seconds" in tool:
                    self.assertTrue(1 <= tool["timeout_seconds"] <= 300, name)
                # Tools AssemblyAI runs itself are the ones that work in a
                # simulation; publish.py warns about any others.
                self.assertIn("http", tool, f"{name}: {tool['name']} has no http block")

    def test_turn_detection_stays_within_documented_ranges(self):
        for name, agent in self.agents():
            turn = ((agent.get("input") or {}).get("turn_detection") or {})
            if "vad_threshold" in turn:
                self.assertTrue(0.0 <= turn["vad_threshold"] <= 1.0, name)
            if "min_silence" in turn and "max_silence" in turn:
                self.assertLess(turn["min_silence"], turn["max_silence"], name)


class JsoncTests(unittest.TestCase):
    def test_comments_and_trailing_commas_are_stripped(self):
        parsed = lib.parse_jsonc('''
        {
          // a line comment
          "name": "a // b",   /* and a block one */
          "list": [1, 2,],
        }
        ''')
        self.assertEqual(parsed, {"name": "a // b", "list": [1, 2]})


class EnvTests(unittest.TestCase):
    def test_agent_id_key_per_file(self):
        self.assertEqual(lib.agent_id_key("support-line"), "AGENT_ID_SUPPORT_LINE")

    def test_bare_agent_id_overrides_per_file_keys(self):
        with mock.patch.dict(os.environ, {"AGENT_ID_SUPPORT_LINE": "per-file", "AGENT_ID": "override"}):
            self.assertEqual(lib.stored_agent_id("support-line"), "override")
        with mock.patch.dict(os.environ, {"AGENT_ID_SUPPORT_LINE": "per-file"}, clear=False):
            os.environ.pop("AGENT_ID", None)
            self.assertEqual(lib.stored_agent_id("support-line"), "per-file")


if __name__ == "__main__":
    unittest.main()
