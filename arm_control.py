"""Shared exclusive access and checked angle reads for local arm tools."""

import fcntl
import math
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


def read_angle_retry(arm, servo, attempts=3):
    # The SDK returns None for a missed answer. The vendor examples read again
    # once; a servo that never answers still fails after the last attempt.
    value = None
    for attempt in range(attempts):
        value = arm.Arm_serial_servo_read(servo)
        if value is not None:
            break
        if attempt + 1 < attempts:
            time.sleep(0.01)
    return value


NO_ANSWER = -73  # read_any result when the board answers raw 0 (servo gave no answer)


def outside_angle(arm, servo, attempts=3):
    """Angle of a servo the SDK calls unavailable because it sits outside its range.

    Arm_serial_servo_read returns None both for "no answer" and for "outside
    0..180". read_any keeps the raw value, so a steady reading that is not the
    no-answer value is the real position. Returns None when it is not trustworthy.
    """
    values = []
    for _ in range(attempts):
        try:
            value = arm.Arm_serial_servo_read_any(servo)
        except Exception:
            return None
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= NO_ANSWER):
            return None
        values.append(value)
        time.sleep(0.01)
    if max(values) - min(values) > 2:
        return None
    mean = sum(values) / len(values)
    if servo == 5:  # read_any always scales as 900..3100 -> 0..180: undo it.
        return 270.0 * (900 + mean * 2200 / 180.0 - 380) / 3320.0
    return 180 - mean if servo in (2, 3, 4) else mean


def read_angles(arm, recover=False):
    """All six angles. With recover=True a servo outside its range is read through
    read_any (so it can be moved back in) instead of raising."""
    angles = {}
    for servo in range(1, 7):
        value = read_angle_retry(arm, servo)
        limit = 270 if servo == 5 else 180
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= limit):
            value = outside_angle(arm, servo) if recover else None
            if value is None:
                raise RuntimeError("Servo {} angle unavailable; no further motion".format(servo))
        angles[servo] = round(value)
    return angles


@contextmanager
def exclusive_arm():
    directory = Path.home() / '.cache' / 'dof-bot'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'arm.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another dof-bot tool owns the arm')
        # Run before creating Arm_Device, which itself opens /dev/i2c-1.
        result = subprocess.run(
            ['sudo', '-n', 'fuser', '/dev/i2c-1'],
            capture_output=True, text=True, timeout=5)
        if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
            raise RuntimeError(
                'I2C is in use or ownership could not be checked. '
                'Inspect: sudo fuser -v /dev/i2c-1. Stop other arm/OLED/notebook '
                'users first. sudo -n fuser must be permitted.')
        yield
