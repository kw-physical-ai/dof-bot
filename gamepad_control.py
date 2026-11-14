#!/usr/bin/env python3
"""Move several joints at once with a gamepad (layout follows the vendor example).

Left stick  X -> servo 1 (base)      Y -> servo 2 (shoulder)
L1 / L2     -> servo 3 (elbow)  - / +
R1 / R2     -> servo 4 (wrist)  - / +
Keys 2 / 4  -> servo 5 (roll)   + / -     (button numbers 1 / 3)
Keys 1 / 3  -> servo 6 (gripper) + / -    (button numbers 0 / 2)
Default: USB + OV5647 side preview on port 8080, 3-degree steps.
SELECT stops the program. --steps can enable START step cycling.

Starts at the current pose. --align-start explicitly enables alignment to 90 degrees.
Manual inputs held during alignment must be released before use. A failed movement blocks
only that joint/direction; reverse input and other joints remain available.
Unreadable joints are disabled; other joints remain available.
Wrong-way movement pauses all motion.
Button numbers are Linux joystick numbers: check them
with joystick_control.py (input-only mode) first.
"""

import argparse
import array
import fcntl
import os
import select
import signal
import subprocess
import sys
import time

from arm_control import exclusive_arm, open_arm, read_angles
from camera_stream import camera_arg
from joystick_control import AXIS, BUTTON, EVENT, INIT

DEADZONE = 10000
EDGE = 3
FEEDBACK_TOLERANCE = 2  # Integer SDK feedback jitters by 0..2 degrees at rest.
RANGES = {1: (0, 180), 2: (0, 180), 3: (0, 180), 4: (0, 180), 5: (0, 270), 6: (0, 180)}
# servo -> (buttons that raise the angle, buttons that lower it)
BUTTON_MAP = {6: ((0,), (2,)), 5: ((1,), (3,)), 3: ((6,), (4,)), 4: ((7,), (5,))}


class Pad:
    def __init__(self, stop=8, step_button=9, steps=(3, 6, 10), invert=(), phase_button=None):
        self.stop, self.step_button = stop, step_button
        self.steps, self.step_index = tuple(steps), 0
        self.invert = set(invert)
        self.axes, self.buttons = {}, {}
        self.ready = False
        self.stopped = False
        self.phase_button = phase_button
        self.phase_requested = False

    @property
    def step(self):
        return self.steps[self.step_index]

    def wanted(self):
        want = {}

        def add(servo, direction):
            if direction:
                want[servo] = want.get(servo, 0) + direction

        x, y = self.axes.get(0, 0), self.axes.get(1, 0)
        add(1, -1 if x > DEADZONE else 1 if x < -DEADZONE else 0)
        add(2, 1 if y > DEADZONE else -1 if y < -DEADZONE else 0)
        for servo, (raise_keys, lower_keys) in BUTTON_MAP.items():
            add(servo, sum(1 for b in raise_keys if self.buttons.get(b))
                - sum(1 for b in lower_keys if self.buttons.get(b)))
        return {servo: (-d if servo in self.invert else d)
                for servo, d in want.items() if d}

    def event(self, kind, number, value):
        initial = bool(kind & INIT)
        kind &= ~INIT
        if initial:
            # Initial state or queue overflow: require everything released again.
            self.ready = False
        if kind == AXIS:
            self.axes[number] = value
        elif kind == BUTTON:
            was_pressed = self.buttons.get(number, False)
            self.buttons[number] = bool(value)
            if number == self.phase_button and value and not was_pressed and not initial:
                self.phase_requested = True
            if number == self.stop and value:
                self.stopped = True
            if (number == self.step_button and value and not initial
                    and self.ready and not self.wanted()):
                self.step_index = (self.step_index + 1) % len(self.steps)

    def settle(self):
        # Called after the whole input queue is drained.
        if 0 in self.axes and 1 in self.axes and not self.wanted() and not self.stopped:
            self.ready = True

    def request(self):
        return self.wanted() if self.ready and not self.stopped else {}


def plan(angles, request, step, limits):
    """Target angle per requested servo; a joint outside its limit may only move back in."""
    targets = {}
    for servo, direction in request.items():
        angle = angles[servo]
        if angle is None:
            continue
        low, high = limits[servo]
        target = min(max(angle + direction * step, min(low, angle)), max(high, angle))
        sdk_low, sdk_high = RANGES[servo]
        if target != angle and sdk_low <= target <= sdk_high:
            targets[servo] = target
    return targets


class InputError(RuntimeError):
    """The controller itself failed: this ends the program."""


def drain(fd, pad):
    for _ in range(4096):
        try:
            data = os.read(fd, EVENT.size)
        except BlockingIOError:
            pad.settle()
            return
        if len(data) != EVENT.size:
            raise InputError('Joystick disconnected or incomplete event')
        _, value, kind, number = EVENT.unpack(data)
        pad.event(kind, number, value)
    raise InputError('Input queue could not be drained; stopping')


def align_start(fd, pad, arm, limits, stream, home=None):
    """Move to the requested all-90 start pose before accepting manual motion."""
    home = home if home is not None else {servo: 90 for servo in RANGES}
    if any(not limits[s][0] <= target <= limits[s][1] for s, target in home.items()):
        raise RuntimeError('Start pose is outside configured limits')
    print('Aligning selected joints: {}. SELECT / Ctrl+C cancels.'.format(home), flush=True)
    if stream:
        stream.set_status(['ALIGNING {}'.format(home), 'SELECT / Ctrl+C cancels'])
    skipped = set()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        drain(fd, pad)
        if pad.stopped:
            return False
        if stream and stream.error:
            raise RuntimeError(stream.error + '; alignment stopped')
        angles = read_angles(arm, recover=True, partial=True)
        for servo, q in angles.items():
            if servo in home and q is None and servo not in skipped:
                skipped.add(servo)
                print('S{} unreadable: skipping automatic alignment'.format(servo), flush=True)
        request = {s: (1 if home[s] > angles[s] else -1) for s in home
                   if s not in skipped and angles[s] is not None and abs(angles[s] - home[s]) > 2}
        if not request:
            if all(q is None for q in angles.values()):
                raise RuntimeError('All 6 servos unreadable: check motor power/board connection; alignment stopped')
            print('Alignment incomplete for {}. Manual control ready.'.format(
                sorted(skipped)) if skipped else 'Start pose reached. Gamepad ready.', flush=True)
            # Inputs held during alignment cannot start manual motion.
            pad.ready = False
            drain(fd, pad)
            return not pad.stopped
        targets = plan(angles, request, 3, limits)
        if not targets:
            raise RuntimeError('Cannot approach start pose within limits')
        drain(fd, pad)
        if pad.stopped:
            return False
        # Finish one joint's step before writing another: avoid simultaneous
        # startup load and make the failing joint unambiguous.
        targets = dict([next(iter(targets.items()))])
        for servo, target in targets.items():
            arm.Arm_serial_servo_write(servo, target, 300)
        until = time.monotonic() + .4
        while time.monotonic() < until:
            select.select([fd], [], [], .02)
            drain(fd, pad)
            if pad.stopped:
                return False
            if stream and stream.error:
                raise RuntimeError(stream.error + '; alignment stopped')
        after = read_angles(arm, recover=True, partial=True)
        for servo, target in targets.items():
            actual = after[servo]
            if actual is not None and (actual - angles[servo]) * request[servo] < -FEEDBACK_TOLERANCE:
                raise RuntimeError('Servo {} moved opposite to alignment: before={} target={} actual={}; '
                                   'stopped. Use --no-align for manual positioning.'.format(
                                       servo, angles[servo], target, actual))
            if actual is None or abs(actual - target) > 2 or actual == angles[servo]:
                skipped.add(servo)
                print('S{} alignment skipped: before={} target={} actual={}; '
                      'manual control remains available'.format(
                          servo, angles[servo], target, actual), flush=True)
    print('Alignment time limit reached. Manual control ready.', flush=True)
    pad.ready = False
    drain(fd, pad)
    return not pad.stopped


def run(fd, pad, arm, limits, stream, recorder=None, speed=40):
    def observe():
        start = time.monotonic()
        values = read_angles(arm, recover=True, partial=True)
        if recorder:
            recorder.state(values, start, time.monotonic())
        return values

    angles = observe()
    if all(q is None for q in angles.values()):
        raise RuntimeError('All 6 servos unreadable: check motor power/board connection. '
                           'No motor commands sent; restarting collection alone will not repair power loss.')
    print('Angles: {}'.format(angles), flush=True)
    for servo, angle in angles.items():
        low, high = RANGES[servo]
        if angle is None:
            print('Servo {} unavailable: only this joint is disabled'.format(servo), flush=True)
        elif not low <= angle <= high:
            print('Servo {} near SDK boundary ({}): inward input only'.format(
                servo, angle), flush=True)
    print('Gamepad active. SELECT / Ctrl+C stops. Release any held inputs.', flush=True)
    pending, stalls = {}, {servo: 0 for servo in RANGES}
    blocked = {servo: set() for servo in RANGES}
    last_limits = set()
    last_write = wait = 0.0
    shown_step = None
    faulted = False
    next_refresh = time.monotonic() + 1
    feedback_lost = False
    while True:
        select.select([fd], [], [], 0.02)
        drain(fd, pad)
        if pad.stopped:
            if recorder:
                recorder.event('stop_button')
            print('Stop button: no further commands')
            return
        if pad.phase_requested:
            pad.phase_requested = False
            if recorder and not pending and pad.ready and not pad.wanted() and not faulted:
                start = time.monotonic()
                angles = observe()
                end = time.monotonic()
                drain(fd, pad)
                if not pad.stopped and pad.ready and not pad.wanted():
                    marked = recorder.mark_grasp_complete(angles, start, end)
                    print('Grasp boundary saved: transport phase.' if marked else
                          'Boundary not saved: already marked, unreadable joints, or recording error.', flush=True)
            else:
                print('Release motion inputs and wait for movement to finish, then press START again.', flush=True)
            # Never send a motor command in response to a phase marker.
            continue
        if shown_step != pad.step:
            if shown_step is not None:
                print('Step {} deg'.format(pad.step), flush=True)
            shown_step = pad.step
        if stream:
            if stream.error:
                raise RuntimeError(stream.error + '; stopping')
            stream.set_status(['gamepad  step {} deg  {}'.format(
                pad.step, 'NO SERVO RESPONSE' if all(q is None for q in angles.values()) else
                'PAUSED' if faulted else 'MOVE' if pad.request() else 'idle'),
                ('REC ERROR: ' + recorder.error if recorder and recorder.error else
                 'REC ' + recorder.path.name if recorder else 'preview only'),
                ('phase: transport' if recorder.grasp_end is not None else
                 'phase: grasp; START after lift') if recorder else '',
                'angles ' + ' '.join('?' if angles[n] is None else str(angles[n]) for n in range(1, 7)),
                'blocked ' + ' '.join('S{}{}'.format(s, '+' if d > 0 else '-')
                    for s in sorted(blocked) for d in sorted(blocked[s]))])
        if pad.ready:
            faulted = False
        try:
            fresh_feedback = False
            if not pending and pad.ready and not pad.wanted():
                for servo in RANGES:
                    if len(blocked[servo]) == 2:
                        blocked[servo].clear()
                        stalls[servo] = 0
                        print('S{} both-direction block cleared after neutral input.'.format(servo), flush=True)
                        if recorder:
                            recorder.event('direction_block_reset', servo=servo, reason='neutral input')
            # Refresh while idle, too: a recovered joint must not stay displayed
            # as unavailable until the operator asks it to move.
            if not pending and not pad.request() and time.monotonic() >= next_refresh:
                angles = observe()
                fresh_feedback = True
                next_refresh = time.monotonic() + 1
            lost = all(q is None for q in angles.values())
            if lost != feedback_lost:
                print('ALL SERVO FEEDBACK LOST: check motor power/board connection.' if lost else
                      'Servo feedback returned.', flush=True)
                if recorder:
                    recorder.event('feedback_lost' if lost else 'feedback_restored')
                feedback_lost = lost
            if pending and time.monotonic() - last_write >= wait:
                angles = observe()
                fresh_feedback = True
                remaining = {}
                if recorder:
                    recorder.event('command_feedback', pending={str(s): list(v) for s, v in pending.items()})
                for servo, (old, target) in pending.items():
                    actual = angles[servo]
                    if actual is None:
                        print('S{} unreadable: joint disabled until feedback returns'.format(
                            servo), flush=True)
                        stalls[servo] = 0
                        continue
                    direction = 1 if target > old else -1
                    if (actual - old) * direction < -FEEDBACK_TOLERANCE:
                        raise RuntimeError('Servo {} moved opposite to command'.format(servo))
                    # A loaded joint may lag one short command. Re-read the
                    # same command before blocking; never flood a stalled motor.
                    if abs(actual - target) > FEEDBACK_TOLERANCE:
                        stalls[servo] += 1
                        if stalls[servo] < 3:
                            remaining[servo] = (old, target)
                            continue
                    else:
                        stalls[servo] = 0
                    if stalls[servo] >= 3:
                        blocked[servo].add(direction)
                        print('S{} {} blocked: movement not confirmed; reverse allowed'.format(
                            servo, '+' if direction > 0 else '-'), flush=True)
                        if recorder:
                            recorder.event('direction_blocked', servo=servo, direction=direction,
                                           before=old, target=target, actual=actual, feedback_checks=3)
                        stalls[servo] = 0
                    elif (actual - old) * direction > 0:
                        if -direction in blocked[servo]:
                            blocked[servo].discard(-direction)
                            print('S{} reverse movement confirmed; direction cleared'.format(
                                servo), flush=True)
                pending = remaining
                if pending:
                    last_write = time.monotonic()
                    wait = .12
            if pending or not pad.request():
                continue
            if not fresh_feedback:
                angles = observe()
            drain(fd, pad)  # Reading takes time: honor a release or stop that arrived.
            if pad.stopped:
                print('Stop button: no further commands')
                return
            request = pad.request()
            allowed = {s: d for s, d in request.items()
                       if angles[s] is not None and d not in blocked[s]}
            targets = plan(angles, allowed, pad.step, limits)
            at_limits = {(s, d) for s, d in allowed.items() if s not in targets}
            for servo, direction in sorted(at_limits - last_limits):
                print('S{} {} limit: reverse allowed'.format(
                    servo, '+' if direction > 0 else '-'), flush=True)
            last_limits = at_limits
            durations = []
            for servo, target in targets.items():
                move_ms = max(80, round(1000 * abs(target - angles[servo]) / speed))
                sent_at = time.monotonic()
                arm.Arm_serial_servo_write(servo, target, move_ms)
                if recorder:
                    recorder.action(servo, target, move_ms, angles, sent_at, time.monotonic())
                durations.append(move_ms)
                pending[servo] = (angles[servo], target)
            if targets:
                last_write = time.monotonic()
                wait = max(durations) / 1000 + 0.04
                print('  '.join('S{}: {}->{}'.format(s, angles[s], t)
                                for s, t in sorted(targets.items())), flush=True)
        except InputError:
            raise
        except RuntimeError as exc:
            if recorder:
                recorder.event('control_fault', message=str(exc))
            # A failed check pauses motion; the program and camera stay up.
            # Motion resumes only after every stick and key was released once.
            pending = {}
            stalls = {servo: 0 for servo in RANGES}
            pad.ready = False
            if not faulted:
                print('WARNING: {}. Motion paused: release every stick and key to '
                      'resume.'.format(exc), flush=True)
                faulted = True


def parse_limits(values):
    limits = {s: (low + EDGE, high - EDGE) for s, (low, high) in RANGES.items()}
    for text in values:
        try:
            servo, low, high = (int(part) for part in text.split(':'))
        except ValueError:
            raise ValueError('--limit must look like 2:40:180')
        full = RANGES.get(servo)
        if full is None or not full[0] <= low < high <= full[1]:
            raise ValueError('--limit {} is outside the SDK range'.format(text))
        limits[servo] = (low, high)
    return limits


def parse_starts(values, limits):
    starts = []
    for text in values:
        try:
            servo, angle = (int(part) for part in text.split(':'))
        except ValueError:
            raise ValueError('--start must look like 1:90')
        if servo not in limits or any(servo == s for s, _ in starts):
            raise ValueError('--start names an unknown or repeated servo')
        if not limits[servo][0] <= angle <= limits[servo][1]:
            raise ValueError('--start {} is outside configured limits'.format(text))
        starts.append((servo, angle))
    return starts


def main(collect_task=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--device', default='/dev/input/js0')
    parser.add_argument('--move', action='store_true', help=argparse.SUPPRESS)
    alignment = parser.add_mutually_exclusive_group()
    alignment.add_argument('--align-start', action='store_true',
                           help='explicitly move all joints toward 90 degrees before manual control')
    alignment.add_argument('--no-align', dest='align_start', action='store_false',
                           help='start at the current pose (default)')
    alignment.add_argument('--start', action='append', default=[], metavar='SERVO:ANGLE',
                           help='explicitly align only these joints, e.g. --start 1:90 (repeatable)')
    parser.set_defaults(align_start=False)
    parser.add_argument('--speed', type=float, default=40,
                        help='command speed in degrees/second, 1..180 (default 40); minimum command duration 80ms')
    parser.add_argument('--stop-button', type=int, default=8)
    parser.add_argument('--step-button', type=int, default=9)
    parser.add_argument('--steps', default='3',
                        help='step sizes in degrees, each 1..10; 1 degree is often below '
                             'what a servo reacts to')
    parser.add_argument('--invert', default='', help='servos whose direction to flip, e.g. 1,2')
    parser.add_argument('--limit', action='append', default=[], metavar='SERVO:MIN:MAX',
                        help='restrict a servo, e.g. 2:40:180 (repeatable)')
    parser.add_argument('--stream', type=int, default=8080, metavar='PORT')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--camera', type=camera_arg, default=0,
                        help='USB index/path, csi0/csi1, or camera list, e.g. 0,csi0')
    parser.add_argument('--side-csi', action='store_true', default=True,
                        help=argparse.SUPPRESS)
    parser.add_argument('--usb-only', dest='side_csi', action='store_false',
                        help='use only USB instead of the default two cameras')
    args = parser.parse_args()
    if args.side_csi and not args.stream:
        parser.error('--side-csi requires --stream PORT')
    try:
        steps = tuple(int(p) for p in args.steps.split(','))
        invert = [int(p) for p in args.invert.split(',') if p]
        limits = parse_limits(args.limit)
        starts = parse_starts(args.start, limits)
    except ValueError as exc:
        parser.error(str(exc))
    if (not steps or not all(1 <= s <= 10 for s in steps)
            or not all(1 <= s <= 6 for s in invert)
            or args.stop_button in range(0, 8) or args.step_button in range(0, 8)
            or args.stop_button == args.step_button
            or not 1 <= args.speed <= 180
            or not 0 <= args.stream <= 65535):
        parser.error('invalid steps/invert/buttons/speed (motion buttons 0..7; speed 1..180)')
    if collect_task is not None and not args.stream:
        parser.error('Collection requires a camera stream')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    print('Gamepad control: motors enabled after neutral input. SELECT / Ctrl+C stops.',
          flush=True)
    fd = None
    recorder = None
    recording_result = 'abort'
    try:
        fd = os.open(args.device, os.O_RDONLY | os.O_NONBLOCK)
        axes, buttons = array.array('B', [0]), array.array('B', [0])
        fcntl.ioctl(fd, 0x80016a11, axes, True)
        fcntl.ioctl(fd, 0x80016a12, buttons, True)
        print('Joystick: {} axes, {} buttons'.format(axes[0], buttons[0]), flush=True)
        if axes[0] < 2 or max(args.stop_button, 7) >= buttons[0]:
            raise RuntimeError('This joystick lacks two axes or the needed buttons')
        step_button = args.step_button if args.step_button < buttons[0] else None
        if collect_task is not None and step_button is None:
            raise RuntimeError('Collection requires the START phase button on this controller')
        pad = Pad(args.stop_button, None if collect_task is not None else step_button,
                  steps if step_button is not None and collect_task is None else steps[:1], invert,
                  phase_button=step_button if collect_task is not None else None)
        with exclusive_arm():
            stream = None
            try:
                if args.stream:
                    from camera_stream import CameraStream
                    stream = CameraStream(args.stream, args.bind, args.camera,
                                              side_csi=args.side_csi)
                    print('Camera view: http://<pi-address>:{}/'.format(args.stream), flush=True)
                arm = open_arm()
                needs_alignment = bool(args.align_start or starts)
                if not needs_alignment or align_start(fd, pad, arm, limits, stream,
                                                      home=dict(starts) if starts else None):
                    if collect_task is not None:
                        if stream is None:
                            raise RuntimeError('Data collection requires camera streaming')
                        from recorder import EpisodeRecorder
                        cameras = ['wrist' if i == 0 else 'external' if i == 1 else 'camera{}'.format(i)
                                   for i in range(len(stream.sources))]
                        recorder = EpisodeRecorder(collect_task, cameras,
                            dict(usb_camera=args.camera, side_csi=args.side_csi,
                                 steps=list(pad.steps), limits=limits, preview_size=list(stream.size),
                                 start_alignment=needs_alignment, start_targets=dict(starts), speed_deg_s=args.speed,
                                 collection_mode='grasp_then_transport'))
                        pad.phase_requested = False
                        for source, name in zip(stream.sources, cameras):
                            source.set_frame_sink(lambda fid, at, frame, camera=name:
                                                  recorder.frame(camera, fid, at, frame))
                        print('Recording: {}'.format(recorder.path), flush=True)
                        # Capture a real initial view from every camera before any manual command.
                        until = time.monotonic() + 3
                        while not all(recorder.counts['frames'].values()) and not recorder.error:
                            select.select([fd], [], [], .02)
                            drain(fd, pad)
                            if pad.stopped:
                                break
                            if time.monotonic() >= until:
                                recorder.error = 'Initial frames missing; episode is incomplete'
                                break
                    pad.phase_requested = False
                    run(fd, pad, arm, limits, stream, recorder=recorder, speed=args.speed)
                    recording_result = 'unreviewed'
            finally:
                if stream:
                    for source in stream.sources:
                        source.set_frame_sink(None)
                try:
                    if recorder:
                        recorder.close(recording_result)
                finally:
                    if stream:
                        stream.close()
        if recorder and sys.stdin.isatty():
            recorder.review()
        return 0
    except KeyboardInterrupt:
        print('Interrupted: no further commands')
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:  # includes InputError
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == '__main__':
    raise SystemExit(main())
