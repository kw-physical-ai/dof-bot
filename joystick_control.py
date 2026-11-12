#!/usr/bin/env python3
"""Inspect a Linux joystick; --move enables hold-to-run single-joint jogging."""

import argparse
import array
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import time

from arm_control import exclusive_arm, open_arm, read_angles
from camera_stream import camera_arg


EVENT = struct.Struct('=IhBB')
BUTTON, AXIS, INIT = 1, 2, 128
STEP_DEGREES = 1
MOVE_MS = 250


class Controls:
    def __init__(self, axis=0, enable=6, stop=7, invert=False,
                 step_button=None, steps=(STEP_DEGREES,)):
        self.axis, self.enable, self.stop = axis, enable, stop
        self.invert = invert
        self.step_button, self.steps, self.step_index = step_button, tuple(steps), 0
        self.axes, self.buttons = {}, {}
        self.servo = 1
        self.ready = False
        self.armed = False
        self.stopped = False

    @property
    def step(self):
        return self.steps[self.step_index]

    def direction(self):
        value = self.axes.get(self.axis, 0)
        direction = 1 if value > 10000 else -1 if value < -10000 else 0
        return -direction if self.invert else direction

    def event(self, kind, number, value):
        initial = bool(kind & INIT)
        kind &= ~INIT
        if initial:
            # Queue overflow also produces INIT events: require a fresh release.
            self.ready = self.armed = False
        if kind == AXIS:
            self.axes[number] = value
        elif kind == BUTTON:
            self.buttons[number] = bool(value)
            if number == self.stop and value:
                self.stopped = True
            if number == self.enable:
                if not value:
                    self.armed = False
                elif not initial and self.ready and self.direction() == 0:
                    self.armed = True
                    self.ready = False
            if (0 <= number <= 5 and value and not initial
                    and not self.buttons.get(self.enable, False)
                    and self.direction() == 0):
                self.servo = number + 1
            if (number == self.step_button and value and not initial
                    and not self.buttons.get(self.enable, False)
                    and self.direction() == 0):
                # Same rule as joint selection: never change while moving.
                self.step_index = (self.step_index + 1) % len(self.steps)

    def settle(self):
        # Called only after the entire input queue is drained.
        if (self.axis in self.axes and self.enable in self.buttons
                and not self.buttons[self.enable] and self.direction() == 0):
            self.ready = True
        if not self.buttons.get(self.enable, False) or self.stopped:
            self.armed = False

    def request(self):
        return self.direction() if self.armed and not self.stopped else 0


class Jogger:
    def __init__(self, arm, clock=time.monotonic):
        self.arm = arm
        self.clock = clock
        self.angles = read_angles(arm)
        self.pending = None
        self.last_write = 0.0
        self.wait = MOVE_MS / 1000 + 0.1
        self.stalls = 0

    def tick(self, servo, direction, refresh=None, step=STEP_DEGREES):
        if self.pending:
            if self.clock() - self.last_write < self.wait:
                return
            old, target, moved_servo = self.pending
            self.angles = read_angles(self.arm)
            actual = self.angles[moved_servo]
            if abs(actual - target) > 2 or (actual - old) * (target - old) < 0:
                raise RuntimeError('Position did not follow command; stopping')
            self.stalls = self.stalls + 1 if actual == old else 0
            if self.stalls >= 3:
                raise RuntimeError('Three commands without measured movement; stopping')
            self.pending = None
        if not direction:
            return
        # Resample on every command: do not accumulate targets from assumed poses.
        self.angles = read_angles(self.arm)
        # Reading I2C can take time: consume release/stop/unplug events again.
        if refresh is not None:
            servo, direction = refresh()
        if not direction:
            return
        old = self.angles[servo]
        limit = 270 if servo == 5 else 180
        # Stop at the SDK range instead of refusing a step that would overshoot.
        target = min(max(old + direction * step, 0), limit)
        if target == old:
            return
        # Larger steps get proportionally more time so the speed stays moderate.
        move_ms = max(MOVE_MS, 100 * abs(target - old))
        self.arm.Arm_serial_servo_write(servo, target, move_ms)
        self.last_write = self.clock()
        self.wait = move_ms / 1000 + 0.1
        self.pending = (old, target, servo)
        print('Servo {}: {} -> {}'.format(servo, old, target), flush=True)


def drain(fd, controls, monitor):
    for _ in range(4096):
        try:
            data = os.read(fd, EVENT.size)
        except BlockingIOError:
            controls.settle()
            return
        if len(data) != EVENT.size:
            raise RuntimeError('Joystick disconnected or incomplete event')
        _, value, kind, number = EVENT.unpack(data)
        controls.event(kind, number, value)
        if monitor:
            print('{} {}={}{}'.format(
                'axis' if kind & AXIS else 'button', number, value,
                ' (initial)' if kind & INIT else ''), flush=True)
    raise RuntimeError('Input queue could not be drained; stopping')


def loop(fd, controls, jogger, seconds, status=None):
    def refresh():
        drain(fd, controls, monitor=False)
        return controls.servo, controls.request()

    start = time.monotonic()
    previous_servo = previous_step = None
    while not seconds or time.monotonic() - start < seconds:
        select.select([fd], [], [], 0.02)
        drain(fd, controls, monitor=jogger is None)
        if controls.stopped:
            print('Stop button: no further commands')
            return
        if previous_servo != controls.servo:
            print('Selected servo {}'.format(controls.servo), flush=True)
            previous_servo = controls.servo
        if previous_step != controls.step:
            if previous_step is not None:
                print('Step {} deg'.format(controls.step), flush=True)
            previous_step = controls.step
        if status:
            status()
        if jogger:
            jogger.tick(controls.servo, controls.request(), refresh, controls.step)


def stream_status(stream, controls, jogger):
    def update():
        if stream.error:
            raise RuntimeError(stream.error + '; stopping')
        stream.set_status([
            'servo {}  step {} deg  {}'.format(
                controls.servo, controls.step,
                'STOP' if controls.stopped else 'MOVE' if controls.armed else 'idle'),
            'angles ' + ' '.join(str(jogger.angles[n]) for n in range(1, 7)),
        ])
    return update


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', default='/dev/input/js0')
    parser.add_argument('--move', action='store_true')
    parser.add_argument('--axis', type=int, default=0)
    parser.add_argument('--enable-button', type=int, default=6)
    parser.add_argument('--stop-button', type=int, default=7)
    parser.add_argument('--invert', action='store_true')
    parser.add_argument('--seconds', type=float, default=0,
                        help='exit after this many seconds; 0 runs until stopped')
    parser.add_argument('--steps', default='3,6,10',
                        help='step sizes in degrees to cycle through, each 1..10 '
                             '(1 degree is often below what a servo reacts to)')
    parser.add_argument('--step-button', type=int, default=8,
                        help='button that cycles the step size (not used if absent)')
    parser.add_argument('--stream', type=int, default=0, metavar='PORT',
                        help='serve the camera view on this port (e.g. 8080)')
    parser.add_argument('--bind', default='0.0.0.0',
                        help='address for --stream; 127.0.0.1 limits it to the Pi')
    parser.add_argument('--camera', type=camera_arg, default=0,
                        help='camera index (0 = /dev/video0) or a device path such as '
                             '/dev/v4l/by-id/...-video-index0')
    parser.add_argument('--side-csi', action='store_true',
                        help='show OV5647 side camera next to USB; requires --stream')
    args = parser.parse_args()
    if args.side_csi and not args.stream:
        parser.error('--side-csi requires --stream PORT')
    try:
        steps = tuple(int(part) for part in args.steps.split(','))
    except ValueError:
        steps = ()
    if (not 0 <= args.seconds <= 86400 or args.axis < 0
            or min(args.enable_button, args.stop_button) < 6
            or args.enable_button == args.stop_button
            or args.step_button < 6
            or args.step_button in (args.enable_button, args.stop_button)
            or not steps or not all(1 <= step <= 10 for step in steps)
            or not 0 <= args.stream <= 65535):
        parser.error('invalid controls/duration/steps; reserve buttons 0..5 for joint selection')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    fd = None
    try:
        fd = os.open(args.device, os.O_RDONLY | os.O_NONBLOCK)
        axes, buttons = array.array('B', [0]), array.array('B', [0])
        fcntl.ioctl(fd, 0x80016a11, axes, True)
        fcntl.ioctl(fd, 0x80016a12, buttons, True)
        print('Joystick: {} axes, {} buttons'.format(axes[0], buttons[0]), flush=True)
        if args.move and (args.axis >= axes[0] or max(args.enable_button, args.stop_button) >= buttons[0]):
            raise RuntimeError('Configured control index is absent on this joystick')
        step_button = args.step_button if args.step_button < buttons[0] else None
        if step_button is None and len(steps) > 1:
            print('No button {}: step stays {} deg'.format(args.step_button, steps[0]),
                  flush=True)
        controls = Controls(args.axis, args.enable_button, args.stop_button,
                            args.invert, step_button, steps if step_button else steps[:1])
        if not args.move:
            print('INPUT ONLY: no SDK is loaded and no motor commands are sent.', flush=True)
            loop(fd, controls, None, args.seconds)
        else:
            with exclusive_arm():
                stream = None
                try:
                    if args.stream:
                        from camera_stream import CameraStream
                        stream = CameraStream(args.stream, args.bind, args.camera,
                                              side_csi=args.side_csi)
                        print('Camera view: http://<pi-address>:{}/'.format(args.stream),
                              flush=True)
                    jogger = Jogger(open_arm())
                    print('Angles: {}'.format(jogger.angles), flush=True)
                    print('Center stick and release enable first. Hold enable, then move stick.', flush=True)
                    loop(fd, controls, jogger, args.seconds,
                         stream_status(stream, controls, jogger) if stream else None)
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
