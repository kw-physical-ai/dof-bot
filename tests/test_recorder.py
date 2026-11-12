import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from recorder import EpisodeRecorder


class Frame:
    def __init__(self, pixels=b'raw without overlay'):
        self.pixels = pixels
    def copy(self):
        return Frame(self.pixels)


def fake_cv(encode=None):
    def default_encode(ext, frame, options):
        return True, types.SimpleNamespace(tobytes=lambda: frame.pixels)
    return types.SimpleNamespace(IMWRITE_JPEG_QUALITY=1, imencode=encode or default_encode)


class RecorderTests(unittest.TestCase):
    def test_raw_copy_timestamps_commands_and_metadata(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('sys.modules', {'cv2': fake_cv()}):
            rec = EpisodeRecorder('종이를 옮겨라', ['front', 'side'], {'steps': [3]}, root=root)
            raw = Frame()
            rec.frame('front', 7, rec.started + .01, raw)
            rec.frame('side', 4, rec.started + .02, raw)
            raw.pixels = b'preview overlay'
            q = {s: 90 for s in range(1, 7)}
            rec.state(q, rec.started + .03, rec.started + .05)
            rec.action(3, 93, 300, q, rec.started + .06, rec.started + .07)
            rec.event('command_feedback')
            rec.close()
            events = [json.loads(line) for line in (rec.path / 'events.jsonl').read_text().splitlines()]
            frames = [e for e in events if e['type'] == 'frame']
            self.assertEqual(len(frames), 2)
            for event in frames:
                self.assertEqual((rec.path / event['file']).read_bytes(), b'raw without overlay')
            state = next(e for e in events if e['type'] == 'state')
            action = next(e for e in events if e['type'] == 'action')
            self.assertLess(state['read_end'], action['t'])
            self.assertEqual(action['servo'], 3)
            self.assertEqual(action['duration_ms'], 300)
            meta = json.loads((rec.path / 'meta.json').read_text())
            self.assertEqual(meta['result'], 'unreviewed')
            self.assertTrue(meta['recording_complete'])
            self.assertEqual(meta['counts']['actions'], 1)
            self.assertEqual(meta['counts']['frames'], {'front': 1, 'side': 1})

    def test_camera_hook_runs_before_preview_overlay(self):
        from camera_stream import CameraStream
        stream = CameraStream.__new__(CameraStream)
        stream.cv2 = fake_cv()
        stream.fps, stream.quality = 15, 80
        stream.stop_event = threading.Event()
        stream.cond = threading.Condition()
        stream.sink_lock = threading.Lock()
        stream.frame_id = stream.version = 0
        stream.measured_fps = 0
        seen = []
        stream.frame_sink = lambda fid, at, frame: seen.append((fid, frame.copy().pixels))
        stream._draw = lambda frame: setattr(frame, 'pixels', b'preview overlay')
        raw = Frame()
        def read():
            stream.stop_event.set()
            return True, raw
        stream.cap = types.SimpleNamespace(read=read)
        with patch('camera_stream.time.monotonic', side_effect=[1, 1.1]):
            stream._capture()
        self.assertEqual(seen, [(1, b'raw without overlay')])
        self.assertEqual(stream.jpeg, b'preview overlay')

    def test_queue_overrun_is_explicit_and_does_not_block_producer(self):
        entered, release = threading.Event(), threading.Event()
        def encode(*args):
            entered.set()
            release.wait(3)
            return True, types.SimpleNamespace(tobytes=lambda: b'jpg')
        with tempfile.TemporaryDirectory() as root, patch.dict('sys.modules', {'cv2': fake_cv(encode)}):
            rec = EpisodeRecorder('test', ['front'], {}, root=root, queue_size=1)
            rec.frame('front', 1, time.monotonic(), Frame())
            self.assertTrue(entered.wait(1))
            rec.event('one')
            rec.event('overflow')
            self.assertIn('queue full', rec.error)
            release.set()
            rec.close()
            meta = json.loads((rec.path / 'meta.json').read_text())
            self.assertFalse(meta['recording_complete'])
            self.assertIn('queue full', meta['error'])

    def test_writer_error_and_abort_are_preserved(self):
        def fail(*args):
            raise OSError('disk full')
        with tempfile.TemporaryDirectory() as root, patch.dict('sys.modules', {'cv2': fake_cv(fail)}):
            rec = EpisodeRecorder('test', ['front'], {}, root=root)
            rec.frame('front', 1, time.monotonic(), Frame())
            rec.close('abort')
            meta = json.loads((rec.path / 'meta.json').read_text())
            self.assertFalse(meta['recording_complete'])
            self.assertEqual(meta['result'], 'abort')
            self.assertIn('disk full', meta['error'])

    def test_missing_feedback_is_an_issue_not_a_fabricated_value(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('sys.modules', {'cv2': fake_cv()}):
            rec = EpisodeRecorder('test', ['front'], {}, root=root)
            rec.state({s: None for s in range(1, 7)}, time.monotonic(), time.monotonic())
            rec.close()
            meta = json.loads((rec.path / 'meta.json').read_text())
            self.assertIn('unreadable_joint_feedback', meta['issues'])
            event = json.loads((rec.path / 'events.jsonl').read_text().splitlines()[0])
            self.assertEqual(event['joints'], [None] * 6)
