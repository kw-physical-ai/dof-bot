"""Dual preview tests without opening camera or motor devices."""
import threading
import time
import types
import unittest
import urllib.request
from unittest.mock import Mock, patch

from camera_stream import CameraStream, CSICapture


class Frame:
    shape = (480, 640, 3)


class Capture:
    def __init__(self, *args):
        self.released = False

    def set(self, *args):
        pass

    def read(self):
        time.sleep(0.01)
        return True, Frame()

    def release(self):
        self.released = True


def fake_cv():
    return types.SimpleNamespace(
        CAP_ANY=0, CAP_V4L2=200, CAP_PROP_FOURCC=6,
        CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_BUFFERSIZE=38,
        IMWRITE_JPEG_QUALITY=1, FONT_HERSHEY_SIMPLEX=0, LINE_AA=16,
        VideoCapture=Capture, VideoWriter_fourcc=lambda *a: 0,
        putText=lambda *a: None,
        imencode=lambda *a: (True, types.SimpleNamespace(tobytes=lambda: b'jpeg')))


class DualCameraTests(unittest.TestCase):
    def test_single_http_server_two_streams_and_side_fault(self):
        side = Capture()
        with patch.dict('sys.modules', {'cv2': fake_cv()}), \
                patch('camera_stream.device_name', return_value='USB camera'), \
                patch('camera_stream.CSICapture', return_value=side):
            stream = CameraStream(0, bind='127.0.0.1', side_csi=True)
            try:
                base = 'http://127.0.0.1:{}'.format(stream.server.server_port)
                with urllib.request.urlopen(base, timeout=2) as response:
                    self.assertIn(b'/side/stream', response.read())
                for path in ['/stream', '/side/stream']:
                    with urllib.request.urlopen(base + path, timeout=2) as response:
                        self.assertIn(b'jpeg', response.read(64))
                self.assertIsNone(stream.side.server)
                stream.side.last_frame_at = time.monotonic() - 4
                stream.side.stop_event.set()
                stream.side.threads[0].join(1)
                stream.side.last_frame_at = time.monotonic() - 4
                self.assertIn('Side camera', stream.error)
            finally:
                stream.close()
            self.assertTrue(side.released)
            self.assertTrue(stream.cap.released)

    def test_side_failure_releases_usb(self):
        usb = Capture()
        cv = fake_cv()
        cv.VideoCapture = lambda *a: usb
        with patch.dict('sys.modules', {'cv2': cv}), \
                patch('camera_stream.device_name', return_value='USB camera'), \
                patch('camera_stream.CSICapture', side_effect=RuntimeError('missing OV5647')):
            with self.assertRaisesRegex(RuntimeError, 'missing OV5647'):
                CameraStream(0, side_csi=True)
        self.assertTrue(usb.released)

    def test_sensor_selection_uses_model_not_index_zero(self):
        camera = Mock()
        cls = Mock(return_value=camera)
        cls.global_camera_info.return_value = [
            {'Model': 'imx219', 'Num': 5}, {'Model': 'ov5647', 'Num': 3}]
        with patch.dict('sys.modules', {'picamera2': types.SimpleNamespace(Picamera2=cls)}):
            cap = CSICapture(640, 480, 15)
            cls.assert_called_once_with(1)
            camera.create_video_configuration.assert_called_once_with(
                main={'size': (640, 480), 'format': 'RGB888'},
                controls={'FrameRate': 15}, buffer_count=4)
            cap.read()
            camera.capture_array.assert_called_once_with(
                'main', wait=False)
            camera.capture_array.return_value.get_result.assert_called_once_with(timeout=3)
            cap.release()
            camera.stop.assert_called_once()
            camera.close.assert_called_once()

    def test_side_fault_blocks_joystick_before_motor_tick(self):
        from joystick_control import stream_status
        stream = Mock()
        stream.error = 'Side camera: Camera frame timeout'
        jogger = Mock()
        with self.assertRaisesRegex(RuntimeError, 'Side camera'):
            stream_status(stream, Mock(), jogger)()
        jogger.tick.assert_not_called()

    def test_sensor_missing_is_explicit(self):
        cls = Mock()
        cls.global_camera_info.return_value = []
        with patch.dict('sys.modules', {'picamera2': types.SimpleNamespace(Picamera2=cls)}):
            with self.assertRaisesRegex(RuntimeError, 'Expected one OV5647'):
                CSICapture(640, 480, 15)
        cls.assert_not_called()
