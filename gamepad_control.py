#!/usr/bin/env python3
"""Move several joints at once with a gamepad (layout follows the vendor example).

Left stick  X -> servo 1 (base)      Y -> servo 2 (shoulder)
L1 / L2     -> servo 3 (elbow)  - / +
R1 / R2     -> servo 4 (wrist)  - / +
Keys 2 / 4  -> servo 5 (roll)   + / -     (button numbers 1 / 3)
Keys 1 / 3  -> servo 6 (gripper) + / -    (button numbers 0 / 2)
START cycles the step size, SELECT stops the program.

Unlike the vendor example this starts from the angles it reads, ignores input
until the sticks are centered and buttons released, and stops if a joint does
not follow its command. Button numbers are Linux joystick numbers: check them
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

from arm_control import exclusive_arm, read_angles
from joystick_control import AXIS, BUTTON, EVENT, INIT, MOVE_MS

DEADZONE = 10000
RANGES = {1: (0, 180), 2: (0, 180), 3: (0, 180), 4: (0, 180), 5: (0, 270), 6: (0, 180)}
# servo -> (buttons that raise the angle, buttons that lower it)
BUTTON_MAP = {6: ((0,), (2,)), 5: ((1,), (3,)), 3: ((6,), (4,)), 4: ((7,), (5,))}


class Pad:
    def __init__(self, stop=8, step_button=9, steps=(1, 3, 6), invert=()):
        self.stop, self.step_button = stop, step_button
        self.steps, self.step_index = tuple(steps), 0
        self.invert = set(invert)
        self.axes, self.buttons = {}, {}
        self.ready = False
        self.stopped = False

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
            self.buttons[number] = bool(value)
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
        low, high = limits[servo]
        target = min(max(angle + direction * step, min(low, angle)), max(high, angle))
        if target != angle:
            targets[servo] = target
    return targets


def drain(fd, pad):
    for _ in range(4096):
        try:
            data = os.read(fd, EVENT.size)
        except BlockingIOError:
            pad.settle()
            return
        if len(data) != EVENT.size:
            raise RuntimeError('Joystick disconnected or incomplete event')
        _, value, kind, number = EVENT.unpack(data)
        pad.event(kind, number, value)
    raise RuntimeError('Input queue could not be drained; stopping')


def run(fd, pad, arm, limits, stream):
    angles = read_angles(arm)
    print('Angles: {}'.format(angles), flush=True)
    print('Center the sticks and release all keys first.', flush=True)
    pending, stalls = {}, {servo: 0 for servo in RANGES}
    last_write = wait = 0.0
    shown_step = None
    while True:
        select.select([fd], [], [], 0.02)
        drain(fd, pad)
        if pad.stopped:
            print('Stop button: no further commands')
            return
        if shown_step != pad.step:
            if shown_step is not None:
                print('Step {} deg'.format(pad.step), flush=True)
            shown_step = pad.step
        if stream:
            if stream.error:
                raise RuntimeError(stream.error + '; stopping')
            stream.set_status(['gamepad  step {} deg  {}'.format(
                pad.step, 'MOVE' if pad.request() else 'idle'),
                'angles ' + ' '.join(str(angles[n]) for n in range(1, 7))])
        if pending and time.monotonic() - last_write >= wait:
            angles = read_angles(arm)
            for servo, (old, target) in pending.items():
                actual = angles[servo]
                if abs(actual - target) > 2 or (actual - old) * (target - old) < 0:
                    raise RuntimeError('Servo {} did not follow the command; stopping'.format(servo))
                stalls[servo] = stalls[servo] + 1 if actual == old else 0
                if stalls[servo] >= 3:
                    raise RuntimeError('Servo {}: three commands without movement; stopping'.format(servo))
            pending = {}
        if pending or not pad.request():
            continue
        angles = read_angles(arm)
        drain(fd, pad)  # Reading takes time: honor a release or stop that arrived.
        if pad.stopped:
            print('Stop button: no further commands')
            return
        targets = plan(angles, pad.request(), pad.step, limits)
        durations = []
        for servo, target in targets.items():
            move_ms = max(MOVE_MS, 100 * abs(target - angles[servo]))
            arm.Arm_serial_servo_write(servo, target, move_ms)
            durations.append(move_ms)
            pending[servo] = (angles[servo], target)
        if targets:
            last_write = time.monotonic()
            wait = max(durations) / 1000 + 0.1
            print('  '.join('S{}: {}->{}'.format(s, angles[s], t)
                            for s, t in sorted(targets.items())), flush=True)


def parse_limits(values):
    limits = dict(RANGES)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--device', default='/dev/input/js0')
    parser.add_argument('--move', action='store_true', help='actually move the motors')
    parser.add_argument('--stop-button', type=int, default=8)
    parser.add_argument('--step-button', type=int, default=9)
    parser.add_argument('--steps', default='1,3,6', help='step sizes in degrees, each 1..10')
    parser.add_argument('--invert', default='', help='servos whose direction to flip, e.g. 1,2')
    parser.add_argument('--limit', action='append', default=[], metavar='SERVO:MIN:MAX',
                        help='restrict a servo, e.g. 2:40:180 (repeatable)')
    parser.add_argument('--stream', type=int, default=0, metavar='PORT')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--camera', type=int, default=0)
    args = parser.parse_args()
    try:
        steps = tuple(int(p) for p in args.steps.split(','))
        invert = [int(p) for p in args.invert.split(',') if p]
        limits = parse_limits(args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    if (not steps or not all(1 <= s <= 10 for s in steps)
            or not all(1 <= s <= 6 for s in invert)
            or args.stop_button in range(0, 8) or args.step_button in range(0, 8)
            or args.stop_button == args.step_button
            or not 0 <= args.stream <= 65535):
        parser.error('invalid steps/invert/buttons (0..7 are used for motion)')
    if not args.move:
        parser.error('add --move to enable the motors')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    fd = None
    try:
        fd = os.open(args.device, os.O_RDONLY | os.O_NONBLOCK)
        axes, buttons = array.array('B', [0]), array.array('B', [0])
        fcntl.ioctl(fd, 0x80016a11, axes, True)
        fcntl.ioctl(fd, 0x80016a12, buttons, True)
        print('Joystick: {} axes, {} buttons'.format(axes[0], buttons[0]), flush=True)
        if axes[0] < 2 or max(args.stop_button, 7) >= buttons[0]:
            raise RuntimeError('This joystick lacks two axes or the needed buttons')
        step_button = args.step_button if args.step_button < buttons[0] else None
        pad = Pad(args.stop_button, step_button, steps if step_button else steps[:1], invert)
        with exclusive_arm():
            stream = None
            try:
                if args.stream:
                    from camera_stream import CameraStream
                    stream = CameraStream(args.stream, args.bind, args.camera)
                    print('Camera view: http://<pi-address>:{}/'.format(args.stream), flush=True)
                from Arm_Lib import Arm_Device
                run(fd, pad, Arm_Device(), limits, stream)
            finally:
                if stream:
                    stream.close()
        return 0
    except KeyboardInterrupt:
        print('Interrupted: no further commands')
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == '__main__':
    raise SystemExit(main())
