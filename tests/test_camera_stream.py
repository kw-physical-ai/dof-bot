import sys
import time
import types
import unittest
import urllib.error
import urllib.request

import camera_stream
from camera_stream import CameraStream, parse_cameras


class FakeFrame:
    shape = (480, 640, 3)


class FakeBuffer:
    def __init__(self, data):
        self.data = data

    def tobytes(self):
        return self.data


class FakeCap:
    """Camera that delivers frames, or none when `working` is False."""
    opened = []

    def __init__(self, device, backend=None):
        self.device = device
        self.working = device != 'broken'
        self.released = False
        FakeCap.opened.append(self)

    def set(self, *args):
        return True

    def read(self):
        time.sleep(0.005)
        return self.working, FakeFrame()

    def release(self):
        self.released = True


def fake_cv2():
    module = types.SimpleNamespace(
        VideoCapture=FakeCap, CAP_V4L2=200, CAP_ANY=0, CAP_PROP_FOURCC=6,
        CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_BUFFERSIZE=38,
        FONT_HERSHEY_SIMPLEX=0, LINE_AA=16, IMWRITE_JPEG_QUALITY=1,
        VideoWriter_fourcc=lambda *chars: 0,
        putText=lambda *args: None,
    )
    module.imencode = lambda ext, frame, params: (True, FakeBuffer(b'jpeg'))
    return module


def get(port, path):
    with urllib.request.urlopen('http://127.0.0.1:{}{}'.format(port, path), timeout=3) as r:
        return r.status, r.headers.get('Content-Type'), r.read()


class ParseTests(unittest.TestCase):
    def test_numbers_and_paths(self):
        self.assertEqual(parse_cameras('0,1'), [0, 1])
        self.assertEqual(parse_cameras(' 1 '), [1])
        self.assertEqual(parse_cameras('/dev/v4l/by-id/x-index0,0'),
                         ['/dev/v4l/by-id/x-index0', 0])

    def test_rejects_empty_and_duplicate(self):
        for text in ('', '0,', '0,0'):
            with self.assertRaises(ValueError):
                parse_cameras(text)

    def test_page_lists_every_camera(self):
        page = camera_stream.make_page(2).decode()
        self.assertIn('/stream/0', page)
        self.assertIn('/stream/1', page)
        self.assertNotIn('/stream/2', page)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.saved = sys.modules.get('cv2')
        sys.modules['cv2'] = fake_cv2()
        FakeCap.opened = []

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop('cv2', None)
        else:
            sys.modules['cv2'] = self.saved

    def wait_frames(self, stream):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if all(source.jpeg for source in stream.sources):
                return
            time.sleep(0.02)
        self.fail('no frames')

    def test_two_cameras_are_served_separately(self):
        stream = CameraStream(0, '127.0.0.1', [0, 1])
        try:
            port = stream.server.server_address[1]
            self.wait_frames(stream)
            for path in ('/snapshot.jpg', '/snapshot/0.jpg', '/snapshot/1.jpg'):
                status, kind, body = get(port, path)
                self.assertEqual((status, kind, body), (200, 'image/jpeg', b'jpeg'))
            status, kind, body = get(port, '/')
            self.assertIn(b'/stream/1', body)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                get(port, '/snapshot/2.jpg')
            self.assertEqual(cm.exception.code, 404)
            self.assertIsNone(stream.error)
        finally:
            stream.close()
        self.assertTrue(all(cap.released for cap in FakeCap.opened))

    def test_stream_endpoint_sends_multipart(self):
        stream = CameraStream(0, '127.0.0.1', [0, 1])
        try:
            port = stream.server.server_address[1]
            self.wait_frames(stream)
            with urllib.request.urlopen(
                    'http://127.0.0.1:{}/stream/1'.format(port), timeout=3) as r:
                self.assertIn('multipart/x-mixed-replace', r.headers['Content-Type'])
                self.assertIn(b'--frame', r.read(64))
        finally:
            stream.close()

    def test_second_camera_without_frames_stops_start_and_frees_first(self):
        with self.assertRaises(RuntimeError) as cm:
            CameraStream(0, '127.0.0.1', [0, 'broken'])
        self.assertIn('Camera 1', str(cm.exception))
        self.assertTrue(all(cap.released for cap in FakeCap.opened))

    def test_camera_failure_while_running_is_reported(self):
        stream = CameraStream(0, '127.0.0.1', [0, 1])
        try:
            stream.sources[1].cap.working = False
            deadline = time.monotonic() + 5
            while stream.error is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn('Camera 1', stream.error)
        finally:
            stream.close()


if __name__ == '__main__':
    unittest.main()
