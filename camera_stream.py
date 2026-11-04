"""MJPEG preview of the arm camera so the operator can work from the image alone.

The process that streams owns the camera device: do not run another camera
program at the same time. The stream is read-only video and carries no control.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PAGE = (b'<!doctype html><meta charset="utf-8"><title>Dofbot camera</title>'
        b'<body style="margin:0;background:#111">'
        b'<img src="/stream" style="width:100%;max-width:960px">')


class CameraStream:
    def __init__(self, port, bind='0.0.0.0', device=0,
                 width=640, height=480, fps=15, quality=80):
        import cv2  # Imported late so tools without a camera need no OpenCV.
        self.cv2 = cv2
        self.fps, self.quality = fps, quality
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
                    'Camera {} gave no frame. Another program may own it: '
                    'sudo fuser -v /dev/video0'.format(device))
            self.size = (frame.shape[1], frame.shape[0])
            self.lines = []
            self.jpeg = None
            self.version = 0
            self.measured_fps = 0.0
            self.error = None
            self.cond = threading.Condition()
            self.stop_event = threading.Event()
            self.server = self._make_server(bind, port)
        except BaseException:
            self.cap.release()
            raise
        self.threads = [threading.Thread(target=self._capture, daemon=True),
                        threading.Thread(target=self.server.serve_forever, daemon=True)]
        for thread in self.threads:
            thread.start()

    def set_status(self, lines):
        self.lines = list(lines)

    def close(self):
        self.stop_event.set()
        with self.cond:
            self.cond.notify_all()
        self.server.shutdown()
        self.server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)
        self.cap.release()

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
            ok, frame = self.cap.read()
            now = time.monotonic()
            if not ok:
                failures += 1
                if failures >= 30:
                    self.error = 'Camera stopped delivering frames'
                    break
                time.sleep(0.05)
                continue
            failures = 0
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
                        self.reply(PAGE, 'text/html')
                    elif self.path == '/snapshot.jpg' and stream.jpeg:
                        self.reply(stream.jpeg, 'image/jpeg')
                    elif self.path == '/stream':
                        self.serve_stream()
                    else:
                        self.send_error(404)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def serve_stream(self):
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
