#!/usr/bin/env python3
"""Read-only servo health check: ping and repeated angle reads per servo.

Sends no motor command and does not change torque. It shows which servo does
not answer, answers only sometimes, or reports changing values.
"""

import argparse
import sys
import time

from arm_control import exclusive_arm


PING_OK = 0xDA  # Arm_Lib: normal answer; 0 means no data from the servo.
NAMES = {1: 'base', 2: 'shoulder', 3: 'elbow', 4: 'wrist', 5: 'wrist roll', 6: 'gripper'}


def check(arm, servo, reads, gap):
    try:
        ping = arm.Arm_ping_servo(servo)
    except Exception as exc:  # The SDK does not catch I2C errors here.
        ping = 'error: {}'.format(exc)
    values = []
    for _ in range(reads):
        try:
            values.append(arm.Arm_serial_servo_read(servo))
        except Exception:
            values.append(None)
        time.sleep(gap)
    try:
        # Same conversion without the 0..180 range check: shows where a servo
        # that reads None really is (for example -20 or 200 degrees).
        unbounded = arm.Arm_serial_servo_read_any(servo)
    except Exception:
        unbounded = None
    return ping, values, unbounded


def verdict(values):
    good = [value for value in values if value is not None]
    if not good:
        return 'NO DATA'
    if len(good) < len(values):
        # Only the very first read missed: a warm-up miss, not a loose cable.
        if values[0] is None and None not in values[1:]:
            return 'OK' if max(good) - min(good) <= 2 else 'UNSTABLE'
        return 'INTERMITTENT'
    if max(good) - min(good) > 2:
        return 'UNSTABLE'
    return 'OK'


def describe_ping(ping):
    if ping == PING_OK:
        return 'ok'
    if ping in (0, None):
        return 'no answer'
    return str(ping)


def advice(results):
    notes = []
    for servo, (ping, values, state, unbounded) in results.items():
        label = 'Servo {} ({})'.format(servo, NAMES[servo])
        if state == 'NO DATA' and ping != PING_OK:
            notes.append('{}: no answer. Check the servo cable at its port and the '
                         'motor power switch/adapter.'.format(label))
        elif state == 'NO DATA':
            notes.append('{}: answers ping but the angle is outside the readable range '
                         '(unbounded estimate: {}). Look at where the joint physically '
                         'sits: it may be turned past its end.'.format(label, unbounded))
        elif state == 'INTERMITTENT':
            notes.append('{}: answers only sometimes. Reseat the cable; check power.'.format(label))
        elif state == 'UNSTABLE':
            notes.append('{}: value changes. The arm may be moving or limp (torque off).'.format(label))
    return notes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reads', type=int, default=10)
    parser.add_argument('--gap', type=float, default=0.05, help='seconds between reads')
    args = parser.parse_args()
    if not 1 <= args.reads <= 100 or not 0 <= args.gap <= 2:
        parser.error('--reads must be 1..100 and --gap 0..2')
    try:
        with exclusive_arm():
            from Arm_Lib import Arm_Device
            arm = Arm_Device()
            time.sleep(0.2)
            results = {}
            print('servo  name         ping       state         unbounded  reads')
            for servo in range(1, 7):
                ping, values, unbounded = check(arm, servo, args.reads, args.gap)
                state = verdict(values)
                results[servo] = (ping, values, state, unbounded)
                print('{:<6} {:<12} {:<10} {:<13} {:<10} {}'.format(
                    servo, NAMES[servo], describe_ping(ping), state, str(unbounded), values))
    except Exception as exc:
        print('Arm access failed: {}'.format(exc), file=sys.stderr)
        return 1
    notes = advice(results)
    print()
    if notes:
        print('\n'.join(notes))
        return 1
    print('All 6 servos answer steadily. Read only: nothing was moved.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
