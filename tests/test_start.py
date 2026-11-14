"""Selected-joint startup shares the cancellable --align-start implementation."""
import unittest
from unittest.mock import Mock, patch
from gamepad_control import align_start, parse_limits, parse_starts


class FollowArm:
    def __init__(self, stuck=False):
        self.angles = {s: 90 for s in range(1, 7)}
        self.angles[1] = 68
        self.writes = []
        self.stuck = stuck

    def Arm_serial_servo_read(self, servo):
        return self.angles[servo]

    def Arm_serial_servo_write(self, servo, target, duration):
        self.writes.append((servo, target))
        if not self.stuck:
            self.angles[servo] = target


class StartTests(unittest.TestCase):
    def test_parse(self):
        limits = parse_limits([])
        self.assertEqual(parse_starts(['1:90', '2:100'], limits), [(1, 90), (2, 100)])
        for values in [['1'], ['x:90'], ['7:90'], ['1:200'], ['1:0'], ['1:90', '1:95']]:
            with self.assertRaises(ValueError):
                parse_starts(values, limits)

    def execute(self, arm, stopped=False):
        clock = [0.0]
        def now():
            clock[0] += .05
            return clock[0]
        with patch('gamepad_control.time.monotonic', side_effect=now), \
                patch('gamepad_control.select.select'), patch('gamepad_control.drain'):
            return align_start(0, Mock(stopped=stopped), arm, parse_limits([]), None, home={1: 90})

    def test_only_selected_joint_moves_in_small_steps(self):
        arm = FollowArm()
        self.assertTrue(self.execute(arm))
        self.assertLessEqual(abs(arm.angles[1] - 90), 2)
        self.assertEqual({s for s, _ in arm.writes}, {1})
        previous = 68
        for _, target in arm.writes:
            self.assertLessEqual(abs(target - previous), 3)
            previous = target

    def test_stop_prevents_start_movement(self):
        arm = FollowArm()
        self.assertFalse(self.execute(arm, stopped=True))
        self.assertEqual(arm.writes, [])

    def test_stalled_joint_is_not_repeatedly_commanded(self):
        arm = FollowArm(stuck=True)
        self.assertTrue(self.execute(arm))
        self.assertEqual(len(arm.writes), 1)

    def test_already_reached_target_does_not_move(self):
        arm = FollowArm()
        arm.angles[1] = 89
        self.assertTrue(self.execute(arm))
        self.assertEqual(arm.writes, [])
