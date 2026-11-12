"""Local, timestamped demonstration recording; never opens motor/camera devices."""
import json
import queue
import platform
from importlib import metadata
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class EpisodeRecorder:
    def __init__(self, task, cameras, config, root='data/episodes', queue_size=64):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < 512 * 1024**2:
            raise RuntimeError('Recording needs at least 512 MiB free disk space')
        self.path = root / (datetime.now(timezone.utc).strftime('ep_%Y%m%dT%H%M%S_') + uuid.uuid4().hex[:8])
        self.path.mkdir()
        for camera in cameras:
            (self.path / 'frames' / camera).mkdir(parents=True)
        self.started = time.monotonic()
        self.sequence = 0
        self.error = None
        self.closed = False
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=queue_size)
        self.counts = {'frames': {c: 0 for c in cameras}, 'states': 0, 'actions': 0}
        self.issues = set()
        try:
            sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True,
                                          stderr=subprocess.DEVNULL).strip()
            dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True))
        except (OSError, subprocess.CalledProcessError):
            sha, dirty = None, None
        versions = {}
        for package in ['Arm-Lib', 'picamera2', 'opencv-python']:
            try:
                versions[package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                versions[package] = None
        self.meta = dict(environment=dict(python=platform.python_version(),
                                         system=platform.platform(), packages=versions), schema='dofbot-demonstration-v1', task=task, cameras=list(cameras),
                         config=config, code_sha=sha, code_dirty=dirty,
                         started_utc=datetime.now(timezone.utc).isoformat(),
                         result='unreviewed', recording_complete=False,
                         time_basis='host time.monotonic seconds relative to episode start',
                         joint_order=[1, 2, 3, 4, 5, 6], joint_unit='degree',
                         image_format='JPEG without overlays; OpenCV BGR input',
                         synchronization='independent cameras; host receive times, not exposure times')
        self._save_meta()
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def _save_meta(self):
        temp = self.path / 'meta.json.tmp'
        temp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2) + '\n')
        temp.replace(self.path / 'meta.json')

    def _submit(self, event, frame=None):
        # Only a bounded queue operation runs on camera/control threads.
        with self.lock:
            if self.closed or self.error:
                return
            try:
                event['index'] = self.sequence
                self.queue.put_nowait((event, frame))
                self.sequence += 1
            except queue.Full:
                self.error = 'Recording queue full; episode is incomplete'

    def frame(self, camera, frame_id, at, frame):
        if self.error or self.closed or at < self.started:
            return
        event = dict(type='frame', camera=camera, frame_id=frame_id,
                     t=at - self.started, file='frames/{}/{:08d}.jpg'.format(camera, frame_id))
        self._submit(event, frame.copy())

    def state(self, angles, start, end):
        if any(v is None for v in angles.values()):
            self.issues.add('unreadable_joint_feedback')
        self._submit(dict(type='state', t=start - self.started, read_end=end - self.started,
                          joints=[angles[s] for s in range(1, 7)]))

    def action(self, servo, target, duration_ms, before, start, end):
        self._submit(dict(type='action', t=start - self.started, send_end=end - self.started,
                          servo=servo, target=target, duration_ms=duration_ms,
                          joints_before=[before[s] for s in range(1, 7)],
                          meaning='SDK command call returned; execution not assumed successful'))

    def event(self, kind, **values):
        if kind == 'control_fault':
            self.issues.add('control_fault')
        self._submit(dict(type=kind, t=time.monotonic() - self.started, **values))

    def _write(self):
        try:
            import cv2
            with (self.path / 'events.jsonl').open('w') as log:
                while True:
                    item = self.queue.get()
                    if item is None:
                        break
                    event, frame = item
                    if frame is not None:
                        ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                        if not ok:
                            raise RuntimeError('JPEG encoding failed')
                        (self.path / event['file']).write_bytes(encoded.tobytes())
                        self.counts['frames'][event['camera']] += 1
                    elif event['type'] == 'state':
                        self.counts['states'] += 1
                    elif event['type'] == 'action':
                        self.counts['actions'] += 1
                    log.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
                    log.flush()
        except Exception as exc:
            self.error = 'Recording write failed: {}'.format(exc)

    def close(self, result='unreviewed'):
        with self.lock:
            self.closed = True
        # Drain accepted work. A failed writer must not strand shutdown on a full queue.
        while self.thread.is_alive():
            try:
                self.queue.put(None, timeout=.1)
                break
            except queue.Full:
                pass
        self.thread.join()
        if not self.counts['actions']:
            self.issues.add('no_actions')
        if not self.counts['states']:
            self.issues.add('no_states')
        for camera, count in self.counts['frames'].items():
            if not count:
                self.issues.add('no_frames_' + camera)
        self.meta.update(result=result, recording_complete=self.error is None and
                         self.counts['states'] > 0 and all(self.counts['frames'].values()),
                         error=self.error, counts=self.counts, issues=sorted(self.issues),
                         duration=time.monotonic() - self.started)
        self._save_meta()
        print('Episode saved: {}{}'.format(self.path, ' [INCOMPLETE]' if not self.meta['recording_complete'] else ''), flush=True)

    def review(self):
        if self.error:
            print('Recording error: {}'.format(self.error))
        try:
            answer = input('결과: s=성공, f=실패, a=중단, Enter=미검수: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ''
        self.meta['result'] = {'s': 'success', 'f': 'fail', 'a': 'abort'}.get(answer, 'unreviewed')
        self._save_meta()
