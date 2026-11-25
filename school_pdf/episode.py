"""Versioned, event-aligned demonstration records. No robot access."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
import uuid

TASKS = ('paper', 'plastic', 'vinyl')
CONTEXT_FIELDS = ('task', 'destination', 'object_id', 'position_id', 'scene_id',
                  'operator', 'camera_setup', 'bin_layout')
SDK_LIMITS = [[0, 180], [0, 180], [0, 180], [0, 180], [0, 270], [0, 180]]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def config_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def load_task_config(path=None):
    path = Path(path) if path else Path(__file__).with_name('task_config.json')
    config = json.loads(path.read_text())
    if config.get('version') != 1 or set(config.get('tasks', {})) != set(TASKS):
        raise ValueError('Task config must define paper, plastic and vinyl, version 1')
    if not isinstance(config.get('mapping_confirmed'), bool) or not isinstance(config.get('layout_id'), str) or not config['layout_id'].strip():
        raise ValueError('Task config needs mapping_confirmed boolean and layout_id')
    if any(not isinstance(config['tasks'][task], dict) for task in TASKS):
        raise ValueError('Each task must define a destination')
    destinations = [config['tasks'][task].get('destination') for task in TASKS]
    if any(not isinstance(d, str) or not d.strip() for d in destinations) or len(set(destinations)) != 3:
        raise ValueError('Each material needs a distinct destination name')
    return config


def validate_context(context, config):
    if not isinstance(context, dict):
        raise ValueError('수집할 재질·물체·위치·조작자 정보를 입력하세요')
    clean = {}
    for key in CONTEXT_FIELDS:
        value = context.get(key)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
            raise ValueError('수집 정보가 필요합니다: '+key)
        clean[key] = value.strip()
    if clean['task'] not in TASKS:
        raise ValueError('재질은 paper / plastic / vinyl 중 하나여야 합니다')
    if clean['destination'] != config['tasks'][clean['task']]['destination']:
        raise ValueError('선택한 재질의 목적지가 배치 설정과 다릅니다')
    if clean['bin_layout'] != config['layout_id']:
        raise ValueError('목적지 배치 버전을 확인하세요')
    return clean


def code_identity():
    root = Path(__file__).resolve().parents[1]
    try:
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, stderr=subprocess.DEVNULL).decode().strip()
        dirty = bool(subprocess.check_output(['git', 'diff', '--name-only', 'HEAD'], cwd=root).strip())
        sources = {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                   for name in ('school_pdf/data_collect.py', 'school_pdf/episode.py',
                                'school_pdf/remote_gamepad.py', 'arm_control.py')}
        return {'code_sha': sha, 'code_dirty': dirty, 'collection_sources': sources}
    except (OSError, subprocess.CalledProcessError):
        return {'code_sha': 'unknown', 'code_dirty': None}


class EpisodeWriter:
    def __init__(self, path, context, config, settings):
        self.path = Path(path)
        self.count = 0
        self.closed = False
        self.handle = (self.path/'steps.jsonl').open('x')
        self.meta = dict(settings, schema_version=2, status='recording',
                         episode_id=str(uuid.uuid4()), created_at=utc_now(),
                         recording_context=dict(context), task_config=config,
                         task_config_hash=config_digest(config), result='unreviewed',
                         safety_events=[], quality_notes=[], steps=0, **code_identity())
        self.meta['clock'] = 'collector-host-monotonic-ns; compare only inside this episode'
        atomic_json(self.path/'meta.json', self.meta)

    def note(self, issue, safety=True):
        field = 'safety_events' if safety else 'quality_notes'
        if issue not in self.meta[field]:
            self.meta[field].append(issue)

    def capture(self, frames, measured, feedback_start_ns, feedback_end_ns,
                source_ids=None, sources=None, kind='manual', blocked=None):
        import cv2
        if self.closed:
            raise RuntimeError('Episode is already closed')
        observed = time.monotonic_ns()
        if feedback_start_ns > feedback_end_ns or feedback_end_ns > observed:
            raise ValueError('Feedback must precede the observation')
        values = [measured.get(servo) for servo in range(1, 7)]
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)) for v in values):
            raise ValueError('Invalid measured angle')
        if any(v is None for v in values):
            self.note('missing_feedback')
        self.count += 1
        step = dict(id=self.count, kind=kind, observation_ns=observed,
                    feedback_start_ns=feedback_start_ns, feedback_end_ns=feedback_end_ns,
                    measured=values, sources=sources or ['sdk' if v is not None else 'missing' for v in values],
                    images={}, writes=[], complete=False, blocked=blocked or {})
        for index, role in ((0, 'wrist'), (1, 'csi')):
            received, frame = frames[index]
            received_ns = round(received*1_000_000_000)
            if received_ns > observed:
                raise ValueError('Future camera frame')
            name = Path('events')/f'cam{index+1}'/f'{self.count:06d}.jpg'
            target = self.path/name
            target.parent.mkdir(parents=True, exist_ok=True)
            # Preserve the source aspect ratio; resizing belongs to model preprocessing.
            if not cv2.imwrite(str(target), frame, [cv2.IMWRITE_JPEG_QUALITY, 90]):
                raise OSError('Event image write failed: '+str(target))
            step['images'][role] = dict(path=name.as_posix(), receive_ns=received_ns,
                                        source_id=(source_ids or {}).get(index, self.count))
        return step

    def append(self, step, complete=True):
        step['complete'] = complete
        self.handle.write(json.dumps(step, ensure_ascii=False, allow_nan=False)+'\n')
        self.handle.flush()
        if not complete:
            self.note('incomplete_command_batch')

    def finish(self, metadata, aborted=False):
        if self.closed:
            return
        self.handle.flush()
        self.handle.close()
        self.meta.update(metadata)
        self.meta.update(status='aborted' if aborted else 'complete', end_at=utc_now(), steps=self.count)
        atomic_json(self.path/'meta.json', self.meta)
        self.closed = True


class FollowingGuard:
    """Block repeated non-progress in the requested direction, not other joints."""
    def __init__(self, attempts=3):
        self.attempts = attempts
        self.pending = {}
        self.blocked = {}

    def filter(self, measured, request, now=None):
        now = time.monotonic() if now is None else now
        allowed, issues = {}, []
        for servo, direction in request.items():
            angle = measured.get(servo)
            if angle is None:
                continue
            if self.blocked.get(servo) == direction:
                continue
            if servo in self.blocked:
                self.blocked.pop(servo)
                self.pending.pop(servo, None)
            previous = self.pending.get(servo)
            if previous and now < previous['due']:
                continue
            if previous and now >= previous['due'] and previous['direction'] == direction:
                progress = direction*(angle-previous['measured'])
                previous['misses'] = previous['misses']+1 if progress < .5 else 0
                if previous['misses'] >= self.attempts:
                    self.blocked[servo] = direction
                    issues.append('gripper_contact_or_no_progress' if servo == 6 else f'servo_{servo}_no_progress')
                    continue
            allowed[servo] = direction
        return allowed, issues

    def sent(self, servo, direction, measured, duration_ms, now=None):
        now = time.monotonic() if now is None else now
        previous = self.pending.get(servo, {})
        misses = previous.get('misses', 0) if previous.get('direction') == direction else 0
        self.pending[servo] = dict(direction=direction, measured=measured,
                                   due=now+duration_ms/1000+.03, misses=misses)

    def reset(self):
        self.pending.clear()
        self.blocked.clear()
