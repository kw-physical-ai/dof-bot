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

    def test_csi_names_are_kept_as_text(self):
        self.assertEqual(parse_cameras('0,csi0'), [0, 'csi0'])
        self.assertEqual(camera_stream.csi_number('csi1'), 1)
        self.assertIsNone(camera_stream.csi_number('/dev/video0'))
        self.assertIsNone(camera_stream.csi_number(0))

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
            cm.exception.close()
            self.assertIsNone(stream.error)
        finally:
            stream.close()
        self.assertTrue(all(cap.released for cap in FakeCap.opened))

    def test_three_camera_raw_hooks_and_legacy_second_stream(self):
        sys.modules['cv2'].putText = lambda frame, *args: setattr(frame, 'overlay', True)
        stream = CameraStream(0, '127.0.0.1', [0, 2, 4])
        seen = {i: [] for i in range(3)}
        try:
            for i, source in enumerate(stream.sources):
                source.set_frame_sink(lambda fid, at, frame, index=i:
                                      seen[index].append(getattr(frame, 'overlay', False)))
            deadline = time.monotonic() + 3
            while not all(seen.values()) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(all(seen.values()))
            self.assertTrue(all(not overlay for frames in seen.values() for overlay in frames))
            port = stream.server.server_address[1]
            self.assertEqual(get(port, '/snapshot/2.jpg')[0], 200)
            with urllib.request.urlopen('http://127.0.0.1:{}/side/stream'.format(port), timeout=3) as response:
                self.assertIn(b'jpeg', response.read(64))
        finally:
            stream.close()
        self.assertTrue(all(cap.released for cap in FakeCap.opened))

    def test_csi_camera_uses_picamera2_not_opencv(self):
        opened = []

        class FakePicamera2:
            def __init__(self, number):
                opened.append(self)
                self.number, self.started, self.closed = number, False, False

            def create_video_configuration(self, **kwargs):
                return kwargs

            def configure(self, config):
                self.config = config

            def start(self):
                self.started = True

            def capture_array(self, *args, **kwargs):
                time.sleep(0.005)
                return types.SimpleNamespace(get_result=lambda timeout: FakeFrame())

            def stop(self):
                self.started = False

            def close(self):
                self.closed = True

        saved = sys.modules.get('picamera2')
        sys.modules['picamera2'] = types.SimpleNamespace(Picamera2=FakePicamera2)
        try:
            stream = CameraStream(0, '127.0.0.1', [0, 'csi0'])
            try:
                self.wait_frames(stream)
                self.assertEqual(len(FakeCap.opened), 1)  # csi0 never went to OpenCV
                self.assertEqual(opened[0].number, 0)
                self.assertTrue(opened[0].started)
            finally:
                stream.close()
            self.assertTrue(opened[0].closed)
        finally:
            if saved is None:
                sys.modules.pop('picamera2', None)
            else:
                sys.modules['picamera2'] = saved

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
