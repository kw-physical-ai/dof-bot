"""MJPEG preview of one or more arm cameras so the operator can work from the image alone.

The process that streams owns the camera devices: do not run another camera
program at the same time. The stream is read-only video and carries no control.

URLs (camera numbers follow the order given to --camera):
  /                 page showing every camera side by side
  /stream/N         live video of camera N       (/stream is camera 0)
  /snapshot/N.jpg   latest frame of camera N     (/snapshot.jpg is camera 0)
"""

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_cameras(text):
    """'0,1' or '/dev/v4l/by-id/usb-...-index0,0' -> [0, '/dev/v4l/by-id/...']."""
    devices = []
    for part in str(text).split(','):
        part = part.strip()
        if not part:
            raise ValueError('--camera has an empty entry')
        devices.append(int(part) if part.isdigit() else part)
    if len(devices) != len(set(devices)):
        raise ValueError('--camera lists the same camera twice')
    return devices


def make_page(count):
    images = ''.join(
        '<figure style="margin:0;flex:1 1 440px;max-width:960px">'
        '<img src="/stream/{0}" style="width:100%">'
        '<figcaption style="color:#ccc;font:14px sans-serif">camera {0}</figcaption>'
        '</figure>'.format(index) for index in range(count))
    return ('<!doctype html><meta charset="utf-8"><title>Dofbot camera</title>'
            '<body style="margin:0;background:#111">'
            '<div style="display:flex;flex-wrap:wrap;gap:8px">' + images +
            '</div>').encode()


class Source:
    """One camera: its device, capture thread and latest JPEG."""

    def __init__(self, owner, index, device, width, height):
        cv2 = owner.cv2
        self.owner, self.index, self.device = owner, index, device
        self.jpeg = None
        self.version = 0
        self.measured_fps = 0.0
        self.error = None
        backend = getattr(cv2, 'CAP_V4L2', cv2.CAP_ANY)
        self.cap = cv2.VideoCapture(device, backend)
        try:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Small queue: less lag.
            ok, frame = self.cap.read()
            if not ok:
                raise RuntimeError(
                    'Camera {} ({}) gave no frame. Another program may own it: '
                    'sudo fuser -v /dev/video*'.format(index, device))
        except BaseException:
            self.cap.release()
            raise
        self.size = (frame.shape[1], frame.shape[0])
        self.thread = threading.Thread(target=self._capture, daemon=True)

    def start(self):
        self.thread.start()

    def _draw(self, frame):
        cv2 = self.owner.cv2
        lines = ['camera {}'.format(self.index)] + list(self.owner.lines)
        lines.append('cam {:.0f} fps  {}'.format(
            self.measured_fps, time.strftime('%H:%M:%S')))
        for row, text in enumerate(lines):
            origin = (8, 22 + 22 * row)
            cv2.putText(frame, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(frame, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (255, 255, 255), 1, cv2.LINE_AA)

    def _capture(self):
        owner, cv2 = self.owner, self.owner.cv2
        previous = last_sent = time.monotonic()
        failures = 0
        while not owner.stop_event.is_set():
            ok, frame = self.cap.read()
            now = time.monotonic()
            if not ok:
                failures += 1
                if failures >= 30:
                    self.error = 'Camera {} stopped delivering frames'.format(self.index)
                    break
                time.sleep(0.05)
                continue
            failures = 0
            elapsed, previous = now - previous, now
            if elapsed > 0:
                rate = 1.0 / elapsed
                self.measured_fps = rate if not self.measured_fps else (
                    0.9 * self.measured_fps + 0.1 * rate)
            if now - last_sent < 1.0 / owner.fps:
                continue
            last_sent = now
            self._draw(frame)
            ok, buffer = cv2.imencode(
                '.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, owner.quality])
            if ok:
                with owner.cond:
                    self.jpeg = buffer.tobytes()
                    self.version += 1
                    owner.cond.notify_all()
        with owner.cond:
            owner.cond.notify_all()


class CameraStream:
    def __init__(self, port, bind='0.0.0.0', devices=(0,),
                 width=640, height=480, fps=15, quality=80):
        import cv2  # Imported late so tools without a camera need no OpenCV.
        self.cv2 = cv2
        self.fps, self.quality = fps, quality
        self.lines = []
        self.cond = threading.Condition()
        self.stop_event = threading.Event()
        self.sources = []
        self.server = None
        try:
            for index, device in enumerate(devices):
                self.sources.append(Source(self, index, device, width, height))
            if not self.sources:
                raise RuntimeError('No camera given')
            self.server = self._make_server(bind, port)
        except BaseException:
            self._release()
            raise
        for source in self.sources:
            source.start()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    @property
    def error(self):
        for source in self.sources:
            if source.error:
                return source.error
        return None

    @property
    def size(self):
        return self.sources[0].size

    def set_status(self, lines):
        self.lines = list(lines)

    def _release(self):
        for source in self.sources:
            source.cap.release()
        if self.server is not None:
            self.server.server_close()

    def close(self):
        self.stop_event.set()
        with self.cond:
            self.cond.notify_all()
        self.server.shutdown()
        for source in self.sources:
            source.thread.join(timeout=2)
        self.server_thread.join(timeout=2)
        self._release()

    def _make_server(self, bind, port):
        stream = self
        page = make_page(len(self.sources))

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

            def source_for(self, number):
                index = int(number or 0)
                return stream.sources[index] if index < len(stream.sources) else None

            def do_GET(self):
                try:
                    if self.path == '/':
                        self.reply(page, 'text/html')
                        return
                    shot = re.fullmatch(r'/snapshot(?:/(\d+))?\.jpg', self.path)
                    live = re.fullmatch(r'/stream(?:/(\d+))?', self.path)
                    match = shot or live
                    source = self.source_for(match.group(1)) if match else None
                    if source is None:
                        self.send_error(404)
                    elif shot:
                        if source.jpeg:
                            self.reply(source.jpeg, 'image/jpeg')
                        else:
                            self.send_error(503, 'No frame yet')
                    else:
                        self.serve_stream(source)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def serve_stream(self, source):
                self.send_response(200)
                self.send_header('Content-Type',
                                 'multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                seen = -1
                while not stream.stop_event.is_set() and not source.error:
                    with stream.cond:
                        stream.cond.wait_for(
                            lambda: source.version != seen or stream.stop_event.is_set(),
                            timeout=2)
                        jpeg, version = source.jpeg, source.version
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
    """Camera check without any motor: python3 camera_stream.py --port 8080 --camera 0,1"""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--camera', default='0',
                        help='camera numbers or device paths, comma separated, e.g. 0,1 '
                             'or /dev/v4l/by-id/...-video-index0')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('invalid port')
    try:
        devices = parse_cameras(args.camera)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        stream = CameraStream(args.port, args.bind, devices)
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
