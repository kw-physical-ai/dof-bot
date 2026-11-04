#!/usr/bin/env python3
"""Release servo torque so you can turn the arm by hand, with a live angle readout.

ALL joints go limp: hold the arm with your free hand before confirming.
Torque is switched back on when you press Ctrl+C or when the program ends,
which locks every joint at the angle it has at that moment.
"""

import argparse
import sys
import time

from arm_control import exclusive_arm


NAMES = {1: 'base', 2: 'shoulder', 3: 'elbow', 4: 'wrist', 5: 'wrist roll', 6: 'gripper'}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--servo', type=int, default=1, choices=range(1, 7), metavar='1-6',
                        help='servo whose angle is shown while you turn it (default 1)')
    args = parser.parse_args()
    try:
        with exclusive_arm():
            from Arm_Lib import Arm_Device
            arm = Arm_Device()
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
                print('Torque OFF. Turn servo {} ({}) by hand slowly. Aim for 0..180, '
                      'about 90 is centered. Ctrl+C locks the arm in place.'.format(
                          args.servo, NAMES[args.servo]), flush=True)
                while True:
                    try:
                        angle = arm.Arm_serial_servo_read_any(args.servo)
                        value = arm.Arm_serial_servo_read(args.servo)
                    except Exception as exc:
                        angle, value = None, 'error: {}'.format(exc)
                    inside = isinstance(value, (int, float))
                    print('angle {:>5}   read {:>5}   {}'.format(
                        str(angle), str(value), 'in range' if inside else 'OUT OF RANGE'),
                        flush=True)
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
