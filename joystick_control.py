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

from arm_control import exclusive_arm, read_angles


EVENT = struct.Struct('=IhBB')
BUTTON, AXIS, INIT = 1, 2, 128
STEP_DEGREES = 1
MOVE_MS = 250


class Controls:
    def __init__(self, axis=0, enable=6, stop=7, invert=False):
        self.axis, self.enable, self.stop = axis, enable, stop
        self.invert = invert
        self.axes, self.buttons = {}, {}
        self.servo = 1
        self.ready = False
        self.armed = False
        self.stopped = False

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
        self.stalls = 0

    def tick(self, servo, direction, refresh=None):
        if self.pending:
            if self.clock() - self.last_write < MOVE_MS / 1000 + 0.1:
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
        target = old + direction * STEP_DEGREES
        if not 0 <= target <= (270 if servo == 5 else 180):
            return
        self.arm.Arm_serial_servo_write(servo, target, MOVE_MS)
        self.last_write = self.clock()
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


def loop(fd, controls, jogger, seconds):
    def refresh():
        drain(fd, controls, monitor=False)
        return controls.servo, controls.request()

    start = time.monotonic()
    previous_servo = None
    while not seconds or time.monotonic() - start < seconds:
        select.select([fd], [], [], 0.02)
        drain(fd, controls, monitor=jogger is None)
        if controls.stopped:
            print('Stop button: no further commands')
            return
        if previous_servo != controls.servo:
            print('Selected servo {}'.format(controls.servo), flush=True)
            previous_servo = controls.servo
        if jogger:
            jogger.tick(controls.servo, controls.request(), refresh)


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
    args = parser.parse_args()
    if (not 0 <= args.seconds <= 86400 or args.axis < 0
            or min(args.enable_button, args.stop_button) < 6
            or args.enable_button == args.stop_button):
        parser.error('invalid controls/duration; reserve buttons 0..5 for joint selection')
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
        controls = Controls(args.axis, args.enable_button, args.stop_button, args.invert)
        if not args.move:
            print('INPUT ONLY: no SDK is loaded and no motor commands are sent.', flush=True)
            loop(fd, controls, None, args.seconds)
        else:
            with exclusive_arm():
                from Arm_Lib import Arm_Device
                jogger = Jogger(Arm_Device())
                print('Angles: {}'.format(jogger.angles), flush=True)
                print('Center stick and release enable first. Hold enable, then move stick.', flush=True)
                loop(fd, controls, jogger, args.seconds)
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
