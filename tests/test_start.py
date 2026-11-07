import unittest
from unittest import mock

from gamepad_control import go_to_start, parse_limits, parse_starts


class FollowArm:
    """Servo angles follow every write; `stuck` servos ignore writes."""

    def __init__(self, angles, stuck=()):
        self.angles, self.stuck, self.writes = dict(angles), set(stuck), []

    def Arm_serial_servo_read(self, servo):
        return self.angles[servo]

    def Arm_serial_servo_write(self, servo, angle, ms):
        self.writes.append((servo, angle))
        if servo not in self.stuck:
            self.angles[servo] = angle


START = {1: 68, 2: 114, 3: 3, 4: 3, 5: 11, 6: 155}


class StartTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch('gamepad_control.time.sleep')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.limits = parse_limits([])

    def test_parse(self):
        self.assertEqual(parse_starts(['1:90'], self.limits), [(1, 90)])
        for bad in ('1', 'x:90', '7:90', '1:200', '1:0', '1:90', ):
            values = [bad, '1:90'] if bad == '1:90' else [bad]
            with self.assertRaises(ValueError):
                parse_starts(values, self.limits)

    def test_moves_only_the_named_servo_in_small_steps(self):
        arm = FollowArm(START)
        go_to_start(arm, [(1, 90)], self.limits)
        self.assertEqual(arm.angles[1], 90)
        self.assertEqual({servo for servo, _ in arm.writes}, {1})
        self.assertEqual([a for _, a in arm.writes], [78, 90])
        self.assertEqual({k: v for k, v in arm.angles.items() if k != 1},
                         {k: v for k, v in START.items() if k != 1})

    def test_already_at_start_does_not_move(self):
        arm = FollowArm({**START, 1: 89})
        go_to_start(arm, [(1, 90)], self.limits)
        self.assertEqual(arm.writes, [])

    def test_moves_down_too(self):
        arm = FollowArm({**START, 1: 140})
        go_to_start(arm, [(1, 90)], self.limits)
        self.assertEqual(arm.angles[1], 90)
        self.assertLessEqual(max(abs(b - a) for a, b in zip([68, 78], [78, 90])), 12)

    def test_stops_if_the_servo_does_not_follow(self):
        arm = FollowArm(START, stuck={1})
        with self.assertRaises(RuntimeError):
            go_to_start(arm, [(1, 90)], self.limits)
        self.assertEqual(len(arm.writes), 1)


if __name__ == '__main__':
    unittest.main()
