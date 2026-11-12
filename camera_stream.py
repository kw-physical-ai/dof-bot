"""MJPEG preview of the arm camera so the operator can work from the image alone.

The process that streams owns the camera device: do not run another camera
program at the same time. The stream is read-only video and carries no control.
"""

import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def camera_arg(text):
    """--camera value: an index (0 means /dev/video0) or a /dev path.

    A path such as /dev/v4l/by-id/usb-...-video-index0 stays the same USB
    camera even when a Pi 5 adds rp1-cfe/pispbe nodes and the numbers shift.
    """
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    if text.startswith('/dev/'):
        return text
    raise argparse.ArgumentTypeError(
        'camera must be an index like 0 or a path like /dev/video0')


def device_path(device):
    return '/dev/video{}'.format(device) if isinstance(device, int) else device


def device_name(device):
    """Kernel name of the video node, e.g. 'USB 2.0 Camera' or 'rp1-cfe'."""
    try:
        node = Path(device_path(device)).resolve().name
        return (Path('/sys/class/video4linux') / node / 'name').read_text().strip()
    except OSError:
        return None


# Pi 5 creates these nodes for the CSI receiver, ISP and codecs. OpenCV's V4L2
# backend cannot read a CSI camera through them; use picamera2/rpicam for CSI.
NOT_A_USB_CAMERA = ('rp1-cfe', 'pispbe', 'rpi-hevc', 'bcm2835-codec', 'bcm2835-isp')


class CSICapture:
    """Picamera2 adapter. CAM0 is a connector, not a libcamera index."""

    def __init__(self, width, height, fps):
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise RuntimeError('CSI side view requires system Picamera2 on the Pi') from exc
        cameras = Picamera2.global_camera_info()
        matches = [i for i, c in enumerate(cameras)
                   if c.get('Model', '').lower() == 'ov5647']
        if len(matches) != 1:
            raise RuntimeError('Expected one OV5647; run rpicam-hello --list-cameras. '
                               'Configure dtoverlay=ov5647,cam0 for CAM0.')
        self.camera = Picamera2(matches[0])
        try:
            # RGB888 arrays use BGR byte order, matching OpenCV/JPEG encoding.
            config = self.camera.create_video_configuration(
                main={'size': (width, height), 'format': 'RGB888'},
                controls={'FrameRate': fps}, buffer_count=4)
            self.camera.configure(config)
            self.camera.start()
        except BaseException:
            self.camera.close()
            raise

    def read(self):
        job = self.camera.capture_array('main', wait=False)
        frame = job.get_result(timeout=3)
        return frame is not None, frame

    def release(self):
        try:
            self.camera.stop()
        finally:
            self.camera.close()


DUAL_PAGE = (b'<!doctype html><meta charset="utf-8"><title>Dofbot cameras</title>'
             b'<style>body{margin:0;background:#111;color:white;font:18px sans-serif}'
             b'main{display:flex;flex-wrap:wrap}section{flex:1 1 480px}img{width:100%}'
             b'h2{margin:8px}</style><main><section><h2>USB / front</h2>'
             b'<img src="/stream"></section><section><h2>OV5647 / side</h2>'
             b'<img src="/side/stream"></section></main>')


PAGE = (b'<!doctype html><meta charset="utf-8"><title>Dofbot camera</title>'
        b'<body style="margin:0;background:#111">'
        b'<img src="/stream" style="width:100%;max-width:960px">')


class CameraStream:
    def __init__(self, port, bind='0.0.0.0', device=0,
                 width=640, height=480, fps=15, quality=80, side_csi=False,
                 _csi=False, _serve=True):
        import cv2  # Imported late so tools without a camera need no OpenCV.
        self.cv2 = cv2
        self.fps, self.quality = fps, quality
        path = device_path(device)
        name = device_name(device)
        if not _csi and name and name.startswith(NOT_A_USB_CAMERA):
            raise RuntimeError(
                '{} is "{}", a Pi camera-pipeline node, not the USB camera. '
                'List cameras: v4l2-ctl --list-devices'.format(path, name))
        backend = getattr(cv2, 'CAP_V4L2', cv2.CAP_ANY)
        self.side = None
        self.last_frame_at = time.monotonic()
        self.cap = (CSICapture(width, height, fps) if _csi
                    else cv2.VideoCapture(device, backend))
        try:
            if not _csi:
                self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Small queue: less lag.
            ok, frame = self.cap.read()
            if not ok:
                raise RuntimeError(
                    'Camera {} ({}) gave no frame. Another program may own it: '
                    'sudo fuser -v {}. Check the number: v4l2-ctl --list-devices'
                    .format(path, name or 'name unknown', path))
            self.size = (frame.shape[1], frame.shape[0])
            self.lines = []
            self.jpeg = None
            self.version = 0
            self.measured_fps = 0.0
            self._error = None
            self.cond = threading.Condition()
            self.stop_event = threading.Event()
            if side_csi:
                self.side = CameraStream(0, width=width, height=height, fps=fps,
                                         quality=quality, _csi=True, _serve=False)
            self.server = self._make_server(bind, port) if _serve else None
        except BaseException:
            if self.side:
                self.side.close()
            self.cap.release()
            raise
        self.last_frame_at = time.monotonic()
        self.threads = [threading.Thread(target=self._capture, daemon=True)]
        if self.server:
            self.threads.append(threading.Thread(target=self.server.serve_forever, daemon=True))
        for thread in self.threads:
            thread.start()

    @property
    def error(self):
        if self._error:
            return self._error
        if time.monotonic() - self.last_frame_at > 3:
            return 'Camera frame timeout'
        if self.side and self.side.error:
            return 'Side camera: ' + self.side.error
        return None

    def set_status(self, lines):
        self.lines = list(lines)
        if self.side:
            self.side.set_status(lines)

    def close(self):
        self.stop_event.set()
        with self.cond:
            self.cond.notify_all()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)
        try:
            self.cap.release()
        finally:
            if self.side:
                self.side.close()

    def _draw(self, frame):
        cv2 = self.cv2
        lines = list(self.lines)
        lines.append('cam {:.0f} fps  {}'.format(
            self.measured_fps, time.strftime('%H:%M:%S')))
        for row, text in enumerate(lines):
            origin = (8, 22 + 22 * row)
            cv2.putText(frame, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(frame, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (255, 255, 255), 1, cv2.LINE_AA)

    def _capture(self):
        cv2 = self.cv2
        previous = last_sent = time.monotonic()
        failures = 0
        while not self.stop_event.is_set():
            try:
                ok, frame = self.cap.read()
            except Exception as exc:
                self._error = 'Camera capture failed: {}'.format(exc)
                break
            now = time.monotonic()
            if not ok:
                failures += 1
                if failures >= 30:
                    self._error = 'Camera stopped delivering frames'
                    break
                time.sleep(0.05)
                continue
            failures = 0
            self.last_frame_at = now
            elapsed, previous = now - previous, now
            if elapsed > 0:
                rate = 1.0 / elapsed
                self.measured_fps = rate if not self.measured_fps else (
                    0.9 * self.measured_fps + 0.1 * rate)
            if now - last_sent < 1.0 / self.fps:
                continue
            last_sent = now
            self._draw(frame)
            ok, buffer = cv2.imencode(
                '.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
            if ok:
                with self.cond:
                    self.jpeg = buffer.tobytes()
                    self.version += 1
                    self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()

    def _make_server(self, bind, port):
        stream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, kind):
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                try:
                    if self.path == '/':
                        self.reply(DUAL_PAGE if stream.side else PAGE, 'text/html')
                    elif self.path == '/snapshot.jpg' and stream.jpeg and not stream.error:
                        self.reply(stream.jpeg, 'image/jpeg')
                    elif self.path == '/stream':
                        self.serve_stream(stream)
                    elif self.path == '/side/stream' and stream.side:
                        self.serve_stream(stream.side)
                    else:
                        self.send_error(404)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def serve_stream(self, stream):
                self.send_response(200)
                self.send_header('Content-Type',
                                 'multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                seen = -1
                while not stream.stop_event.is_set() and not stream.error:
                    with stream.cond:
                        stream.cond.wait_for(
                            lambda: stream.version != seen or stream.stop_event.is_set(),
                            timeout=2)
                        jpeg, version = stream.jpeg, stream.version
                    if jpeg is None or version == seen:
                        continue
                    seen = version
                    self.wfile.write(
                        b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                        + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n')

        server = ThreadingHTTPServer((bind, port), Handler)
        server.daemon_threads = True
        return server


def main():
    """Camera check without any motor: python3 camera_stream.py --port 8080"""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--camera', type=camera_arg, default=0,
                        help='index (0 = /dev/video0) or device path')
    parser.add_argument('--side-csi', action='store_true',
                        help='show the OV5647 side view next to USB')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('invalid port')
    try:
        stream = CameraStream(args.port, args.bind, args.camera, side_csi=args.side_csi)
    except (OSError, RuntimeError) as exc:
        print(exc)
        return 1
    print('Camera view: http://<pi-address>:{}/  (Ctrl+C to stop)'.format(args.port),
          flush=True)
    try:
        while not stream.error:
            time.sleep(0.5)
            stream.set_status(['camera check only: no motor commands'])
        print(stream.error)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        stream.close()


if __name__ == '__main__':
    raise SystemExit(main())
