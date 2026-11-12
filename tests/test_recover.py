"""Boundary recovery adapted from feat/dual-camera; never send outside SDK range."""
import unittest
from arm_control import outside_angle, read_angles
from gamepad_control import parse_limits, plan

class Arm:
    def Arm_serial_servo_read(self, servo):
        return None if servo == 3 else 90
    def Arm_serial_servo_read_any(self, servo):
        return 181

class RecoveryTests(unittest.TestCase):
    def test_strict_default_and_opt_in_boundary_read(self):
        with self.assertRaises(RuntimeError):
            read_angles(Arm())
        self.assertEqual(read_angles(Arm(), recover=True)[3], -1)

    def test_no_answer_unsteady_invalid_and_far_readings_rejected(self):
        for values in [[-73]*3, [181,190,181], [None]*3, [float('nan')]*3,
                       [True]*3, [206]*3]:
            arm = Arm()
            readings = iter(values)
            arm.Arm_serial_servo_read_any = lambda s: next(readings)
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                read_angles(arm, recover=True)

    def test_inward_only_without_negative_targets(self):
        angles = {s:90 for s in range(1,7)}
        angles[3] = -1
        limits = parse_limits([])
        self.assertEqual(plan(angles,{3:-1},3,limits),{})
        self.assertEqual(plan(angles,{3:1},3,limits),{3:2})
        angles[3] = -3
        self.assertEqual(plan(angles,{3:1},1,limits),{})
        angles[3] = 181
        self.assertEqual(plan(angles,{3:1},3,limits),{})
        self.assertEqual(plan(angles,{3:-1},3,limits),{3:178})

    def test_end_margins(self):
        limits=parse_limits([])
        self.assertEqual(limits[3],(3,177))
        self.assertEqual(limits[5],(3,267))
        angles={s:90 for s in range(1,7)}
        angles[3]=176
        self.assertEqual(plan(angles,{3:1},3,limits),{3:177})


class PartialReadTests(unittest.TestCase):
    def test_unknown_angle_is_none_not_a_guessed_position(self):
        arm=Arm()
        arm.Arm_serial_servo_read_any=lambda s: -73
        angles=read_angles(arm,recover=True,partial=True)
        self.assertIsNone(angles[3])
        self.assertEqual(angles[1],90)
        self.assertEqual(plan(angles,{3:1,1:1},3,parse_limits([])),{1:93})

    def test_far_outside_angle_remains_disabled(self):
        arm=Arm()
        arm.Arm_serial_servo_read_any=lambda s: 245
        self.assertIsNone(read_angles(arm,recover=True,partial=True)[3])
