import unittest
from unittest.mock import Mock, patch
from gamepad_control import align_start, parse_limits


class Arm:
    def __init__(self, angle=87):
        self.angles = {s: angle for s in range(1, 7)}
        self.writes = []
    def Arm_serial_servo_read(self, servo):
        return self.angles[servo]
    def Arm_serial_servo_write(self, servo, target, duration):
        self.writes.append((servo, target, duration))
        self.angles[servo] = target


class AlignTests(unittest.TestCase):
    def execute(self, arm, stopped=False, stream=None):
        clock = [0.0]
        def now():
            clock[0] += .05
            return clock[0]
        pad = Mock(stopped=stopped)
        with patch('gamepad_control.drain'), patch('gamepad_control.select.select'), \
                patch('gamepad_control.time.monotonic', side_effect=now):
            return align_start(0, pad, arm, parse_limits([]), stream)

    def test_alignment_precedes_manual_mode(self):
        arm = Arm()
        self.assertTrue(self.execute(arm))
        self.assertEqual(len(arm.writes), 6)
        self.assertEqual(set(arm.angles.values()), {90})
        self.assertTrue(all(duration==300 for _,_,duration in arm.writes))

    def test_stop_or_camera_fault_prevents_alignment_writes(self):
        arm = Arm()
        self.assertFalse(self.execute(arm, stopped=True))
        with self.assertRaises(RuntimeError):
            self.execute(arm, stream=Mock(error='camera failed'))
        self.assertEqual(arm.writes, [])

    def test_missing_feedback_skips_joint_without_blind_homing(self):
        arm = Arm()
        arm.angles[1] = None
        self.assertTrue(self.execute(arm))
        self.assertTrue(all(s != 1 for s, _, _ in arm.writes))

    def test_failed_alignment_is_not_retried_and_allows_manual_mode(self):
        arm = Arm()
        arm.Arm_serial_servo_write = lambda s,t,d: arm.writes.append((s,t,d))
        self.assertTrue(self.execute(arm))
        self.assertEqual(len(arm.writes), 6)

    def test_one_degree_reverse_jitter_skips_joint_without_aborting(self):
        arm = Arm()
        def write(s, target, duration):
            arm.writes.append((s, target, duration))
            arm.angles[s] = 86 if s == 4 else target
        arm.Arm_serial_servo_write = write
        self.assertTrue(self.execute(arm))
        self.assertEqual(sum(s == 4 for s, _, _ in arm.writes), 1)
        self.assertEqual(arm.angles[6], 90)

    def test_real_reverse_movement_still_aborts_with_measured_values(self):
        arm = Arm()
        arm.Arm_serial_servo_write = lambda s, t, d: arm.angles.update({s: 82})
        with self.assertRaisesRegex(RuntimeError, 'before=87 target=90 actual=82'):
            self.execute(arm)

    def test_complete_feedback_loss_never_claims_manual_ready(self):
        arm = Arm(None)
        with self.assertRaisesRegex(RuntimeError, 'All 6 servos unreadable'):
            self.execute(arm)
        self.assertEqual(arm.writes, [])
