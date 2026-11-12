"""Shared exclusive access and checked angle reads for local arm tools."""

import fcntl
import importlib
import inspect
import math
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


# The expansion board sits on the 40-pin header I2C (GPIO2/3). That bus is
# /dev/i2c-1 on Pi 4 and on Pi 5 (Raspberry Pi OS keeps header buses at low
# numbers). Override only if `ls /dev/i2c-*` and `i2cdetect` show 0x15 elsewhere.
I2C_BUS = int(os.environ.get('DOFBOT_I2C_BUS', '1'))
I2C_DEVICE = '/dev/i2c-{}'.format(I2C_BUS)


def check_i2c_device():
    if not Path(I2C_DEVICE).exists():
        raise RuntimeError(
            '{} not found. Enable I2C (sudo raspi-config nonint do_i2c 0, '
            'then reboot) or set DOFBOT_I2C_BUS to the bus where '
            '"i2cdetect -y N" shows 0x15.'.format(I2C_DEVICE))


def open_arm():
    """Create Arm_Lib.Arm_Device on I2C_BUS.

    Pass the bus explicitly in current SDKs; older SDKs need a temporary
    SMBus redirect while the device object is created.
    """
    check_i2c_device()
    from Arm_Lib import Arm_Device
    if 'i2c_bus' in inspect.signature(Arm_Device).parameters:
        return Arm_Device(i2c_bus=I2C_BUS)
    patched = []
    for name in ('smbus', 'smbus2'):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        original = module.SMBus
        module.SMBus = lambda _bus, _original=original: _original(I2C_BUS)
        patched.append((module, original))
    try:
        return Arm_Device()
    finally:
        for module, original in patched:
            module.SMBus = original


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


def read_angles(arm):
    angles = {}
    for servo in range(1, 7):
        value = read_angle_retry(arm, servo)
        limit = 270 if servo == 5 else 180
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= limit):
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
        check_i2c_device()
        # Run before creating Arm_Device, which itself opens the I2C device.
        result = subprocess.run(
            ['sudo', '-n', 'fuser', I2C_DEVICE],
            capture_output=True, text=True, timeout=5)
        if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
            raise RuntimeError(
                'I2C is in use or ownership could not be checked. '
                'Inspect: sudo fuser -v {}. Stop other arm/OLED/notebook '
                'users first. sudo -n fuser must be permitted '
                '(sudo apt install psmisc if fuser is missing).'.format(I2C_DEVICE))
        yield
