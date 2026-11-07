import unittest

from arm_control import NO_ANSWER, outside_angle, read_angles
from gamepad_control import EDGE, RANGES, parse_limits, plan


class FakeArm:
    """read() is the SDK value (None when unavailable); raw() is read_any."""

    def __init__(self, read, raw=None):
        self.readings, self.raws = dict(read), dict(raw or {})

    def Arm_serial_servo_read(self, servo):
        return self.readings.get(servo)

    def Arm_serial_servo_read_any(self, servo):
        return self.raws.get(servo, NO_ANSWER)


ALL_OK = {1: 90, 2: 58, 3: 177, 4: 40, 5: 6, 6: 148}


def but(base, **changes):
    return {**base, **{int(k[1:]): v for k, v in changes.items()}}


class RecoverTests(unittest.TestCase):
    def test_strict_by_default(self):
        arm = FakeArm(but(ALL_OK, s1=None), {1: 245})
        with self.assertRaises(RuntimeError):
            read_angles(arm)

    def test_out_of_range_base_is_read_through_read_any(self):
        arm = FakeArm(but(ALL_OK, s1=None), {1: 245})
        self.assertEqual(read_angles(arm, recover=True)[1], 245)

    def test_flipped_servos_use_180_minus_raw(self):
        # Seen on the Pi: shoulder unbounded 206 means the SDK angle is -26.
        arm = FakeArm(but(ALL_OK, s2=None), {2: 206})
        self.assertEqual(read_angles(arm, recover=True)[2], -26)

    def test_wrist_roll_uses_its_own_scale(self):
        # Seen on the Pi: read 6 <-> read_any -35 (rounding costs about 1 degree).
        arm = FakeArm(but(ALL_OK, s5=None), {5: -35})
        self.assertAlmostEqual(read_angles(arm, recover=True)[5], 6, delta=2)

    def test_no_answer_is_not_a_position(self):
        # Torque off: every servo answers raw 0, which read_any shows as -73.
        arm = FakeArm({servo: None for servo in range(1, 7)})
        self.assertIsNone(outside_angle(arm, 1))
        with self.assertRaises(RuntimeError):
            read_angles(arm, recover=True)

    def test_unsteady_readings_are_refused(self):
        values = iter([100, 140, 100])

        class Jumpy(FakeArm):
            def Arm_serial_servo_read_any(self, servo):
                return next(values)

        self.assertIsNone(outside_angle(Jumpy({}), 1))

    def test_in_range_servos_are_unchanged(self):
        self.assertEqual(read_angles(FakeArm(ALL_OK), recover=True), ALL_OK)


class OutsideMotionTests(unittest.TestCase):
    def test_outside_servo_can_only_move_back_in(self):
        limits = parse_limits([])
        angles = but(ALL_OK, s1=245)
        self.assertEqual(plan(angles, {1: 1}, 10, limits), {})
        self.assertEqual(plan(angles, {1: -1}, 10, limits), {1: 235})
        angles[1] = -20
        self.assertEqual(plan(angles, {1: -1}, 10, limits), {})
        self.assertEqual(plan(angles, {1: 1}, 10, limits), {1: -10})

    def test_default_limits_keep_a_margin(self):
        limits = parse_limits([])
        for servo, (low, high) in RANGES.items():
            self.assertEqual(limits[servo], (low + EDGE, high - EDGE))
        self.assertEqual(plan(but(ALL_OK, s1=175), {1: -1}, 10, limits), {1: 165})
        self.assertEqual(plan(but(ALL_OK, s1=175), {1: 1}, 10, limits), {1: 177})


if __name__ == '__main__':
    unittest.main()
