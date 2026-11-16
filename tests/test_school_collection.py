"""Dataset integrity tests without opening cameras or sending motor commands."""
import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from school_pdf import data_collect as module


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = patch.object(module, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        self.angles = {1: 90, 2: 100, 3: 80, 4: 40, 5: 90, 6: 30}
        self.read = patch.object(module, 'read_angles', return_value=self.angles.copy())
        self.read.start()
        self.addCleanup(self.read.stop)
        self.collector = module.Collector()
        self.pad = module.gp.Pad(stop=9, step_button=None, steps=(1,), phase_button=8)
        self.addCleanup(lambda: self.collector.finish('test complete'))

    def frames(self):
        for index in range(2):
            self.collector.frame(index, time.monotonic(), np.zeros((480, 640, 3), dtype=np.uint8))

    def test_dual_frames_match_csv_and_are_not_repeated(self):
        self.frames()
        self.collector.begin(None, self.pad)
        self.collector.save()
        self.collector.save()
        self.assertEqual(self.collector.count, 1)
        self.frames()
        self.collector.save()
        self.collector.finish('stop button')
        folder = self.collector.path
        with (folder / 'labels.csv').open() as source:
            rows = list(csv.reader(source))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][2:], [str(self.angles[i]) for i in range(1, 7)])
        for camera in ['cam1', 'cam2']:
            self.assertEqual(sorted(p.name for p in (folder / camera).glob('*.jpg')),
                             ['00001.jpg', '00002.jpg'])
            self.assertEqual(module.cv2.imread(str(folder / camera / '00001.jpg')).shape,
                             (244, 244, 3))
        self.assertEqual(json.loads((folder / 'meta.json').read_text())['frames'], 2)
        self.assertFalse(self.collector.active)

    def test_missing_or_stale_camera_prevents_session(self):
        with self.assertRaises(RuntimeError):
            self.collector.begin(None, self.pad)
        self.frames()
        self.collector.frames[1] = (0, self.collector.frames[1][1])
        with self.assertRaises(RuntimeError):
            self.collector.begin(None, self.pad)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_failed_image_write_does_not_append_label(self):
        self.frames()
        self.collector.begin(None, self.pad)
        with patch.object(module.cv2, 'imwrite', side_effect=[True, False]):
            with self.assertRaises(RuntimeError):
                self.collector.save()
        self.collector.finish('write error')
        with (self.collector.path / 'labels.csv').open() as source:
            self.assertEqual(len(list(csv.reader(source))), 1)
        self.assertEqual(self.collector.count, 0)

    def test_stop_does_not_rewrite_previous_metadata(self):
        self.frames()
        self.collector.begin(None, self.pad)
        self.collector.save()
        self.collector.finish('operator stop')
        before = (self.collector.path / 'meta.json').read_text()
        self.collector.finish('server shutdown')
        self.assertEqual((self.collector.path / 'meta.json').read_text(), before)


if __name__ == '__main__':
    unittest.main()
