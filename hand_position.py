#!/usr/bin/env python3
"""Release servo torque so you can turn the arm by hand, with a live angle readout.

ALL joints go limp: hold the arm with your free hand before confirming.
Torque is switched back on when you press Ctrl+C or when the program ends,
which locks every joint at the angle it has at that moment.
"""

import argparse
import sys
import time

from arm_control import exclusive_arm, open_arm


NAMES = {1: 'base', 2: 'shoulder', 3: 'elbow', 4: 'wrist', 5: 'wrist roll', 6: 'gripper'}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--servo', default='1',
                        help='servo numbers to show while you turn them, e.g. 1 or 1,2 '
                             '(default 1)')
    args = parser.parse_args()
    try:
        servos = sorted({int(part) for part in args.servo.split(',')})
    except ValueError:
        servos = []
    if not servos or not all(1 <= servo <= 6 for servo in servos):
        parser.error('--servo must be numbers 1..6, like 1 or 1,2')
    try:
        with exclusive_arm():
            arm = open_arm()
            time.sleep(0.2)
            print('Every joint will go limp and the arm may drop or swing.')
            print('Clear the space, hold the arm up with one hand, keep cables free.')
            if input('Type "release" to continue: ').strip() != 'release':
                print('Cancelled. Nothing was changed.')
                return 0
            released = False
            try:
                arm.Arm_serial_set_torque(0)
                released = True
                print('Torque OFF. Turn the joints by hand slowly. Servo 1 aims for 0..180 '
                      '(about 90 is centered). Ctrl+C locks the arm in place.', flush=True)
                while True:
                    parts = []
                    for servo in servos:
                        try:
                            angle = arm.Arm_serial_servo_read_any(servo)
                            value = arm.Arm_serial_servo_read(servo)
                        except Exception as exc:
                            angle, value = None, 'err'
                        mark = 'ok' if isinstance(value, (int, float)) else 'OUT'
                        parts.append('[{} {}] angle {:>4} read {:>4} {}'.format(
                            servo, NAMES[servo], str(angle), str(value), mark))
                    print('   '.join(parts), flush=True)
                    time.sleep(0.3)
            except KeyboardInterrupt:
                pass
            finally:
                if released:
                    arm.Arm_serial_set_torque(1)
                    print('\nTorque ON: joints are holding their current angles.')
    except Exception as exc:
        print('Arm access failed: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
