#!/usr/bin/env python3
"""Read Dofbot servo angles, with an optional small single-servo motion."""

import argparse
import sys
import time
from typing import Optional

from arm_control import exclusive_arm, read_angles


MAX_STEP_DEGREES = 10
MOTION_TIME_MS = 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--move", action="store_true", help="enable one small servo motion")
    parser.add_argument("--servo", type=int, choices=range(1, 7), metavar="1-6")
    parser.add_argument("--delta", type=int, help="relative angle in degrees, from -10 to 10")
    args = parser.parse_args()
    if args.move:
        if args.servo is None or args.delta is None:
            parser.error("--move requires --servo and --delta")
        if args.delta == 0 or abs(args.delta) > MAX_STEP_DEGREES:
            parser.error("--delta must be between -10 and 10, excluding 0")
    elif args.servo is not None or args.delta is not None:
        parser.error("--servo and --delta require --move")
    return args


def read_angle(arm: object, servo: int) -> Optional[int]:
    value = arm.Arm_serial_servo_read(servo)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= (270 if servo == 5 else 180):
        return None
    return round(value)


def run(args) -> int:
    try:
        from Arm_Lib import Arm_Device
    except ImportError as exc:
        print(f"Arm_Lib import failed: {exc}", file=sys.stderr)
        return 1

    try:
        arm = Arm_Device()
        time.sleep(0.1)
        angles = read_angles(arm)
        for servo, angle in angles.items():
            print(f"Servo {servo}: {angle if angle is not None else 'unavailable'}")

        if not args.move:
            print("Read only. No motor commands were sent.")
            return 0 if any(angle is not None for angle in angles.values()) else 1

        current = angles[args.servo]
        if current is None:
            print(f"Servo {args.servo} angle is unavailable; motion cancelled.", file=sys.stderr)
            return 1
        target = current + args.delta
        limit = 270 if args.servo == 5 else 180
        if not 0 <= target <= limit:
            print(f"Target {target}° is outside servo {args.servo}'s 0–{limit}° range.", file=sys.stderr)
            return 1
        print(f"Moving servo {args.servo}: {current}° -> {target}° over {MOTION_TIME_MS} ms")
        arm.Arm_serial_servo_write(args.servo, target, MOTION_TIME_MS)
        time.sleep(MOTION_TIME_MS / 1000 + 0.2)
        observed = read_angle(arm, args.servo)
        print(f"Servo {args.servo} after motion: {observed if observed is not None else 'unavailable'}")
        if observed is None or (observed - current) * args.delta <= 0:
            print("Motion was not confirmed by the angle reading.", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(f"Arm communication failed: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    args = parse_args()
    try:
        with exclusive_arm():
            return run(args)
    except Exception as exc:
        print(f"Arm access failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
