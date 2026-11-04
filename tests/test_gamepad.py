import unittest

from gamepad_control import AXIS, BUTTON, INIT, RANGES, Pad, parse_limits, plan


class GamepadTests(unittest.TestCase):
    def neutral(self, **kwargs):
        pad = Pad(**kwargs)
        pad.event(INIT | AXIS, 0, 0)
        pad.event(INIT | AXIS, 1, 0)
        pad.event(INIT | BUTTON, 6, 0)
        pad.settle()
        return pad

    def test_nothing_moves_until_everything_is_released_once(self):
        pad = Pad()
        pad.event(INIT | AXIS, 0, 0)
        pad.event(INIT | AXIS, 1, 0)
        pad.event(INIT | BUTTON, 6, 1)  # L2 held at start
        pad.settle()
        self.assertEqual(pad.request(), {})
        pad.event(BUTTON, 6, 0)
        pad.settle()
        pad.event(BUTTON, 6, 1)
        self.assertEqual(pad.request(), {3: 1})

    def test_sticks_and_keys_map_to_servos(self):
        pad = self.neutral()
        pad.event(AXIS, 0, 32767)   # stick right: base angle down (vendor layout)
        pad.event(AXIS, 1, -32767)  # stick up: shoulder angle down
        pad.event(BUTTON, 0, 1)     # gripper +
        pad.event(BUTTON, 5, 1)     # R1: wrist -
        self.assertEqual(pad.request(), {1: -1, 2: -1, 6: 1, 4: -1})

    def test_opposite_keys_cancel_and_invert_flips(self):
        pad = self.neutral(invert=(1,))
        pad.event(BUTTON, 4, 1)
        pad.event(BUTTON, 6, 1)
        self.assertEqual(pad.request(), {})
        pad.event(AXIS, 0, 32767)
        self.assertEqual(pad.request(), {1: 1})

    def test_stop_and_queue_reset(self):
        pad = self.neutral()
        pad.event(BUTTON, 6, 1)
        self.assertEqual(pad.request(), {3: 1})
        pad.event(INIT | AXIS, 3, 0)   # queue overflow
        self.assertEqual(pad.request(), {})
        pad.event(BUTTON, 8, 1)
        self.assertTrue(pad.stopped)

    def test_step_cycles_only_when_idle(self):
        pad = self.neutral()
        self.assertEqual(pad.step, 3)
        pad.event(BUTTON, 9, 1)
        pad.event(BUTTON, 9, 0)
        self.assertEqual(pad.step, 6)
        pad.event(BUTTON, 6, 1)
        pad.event(BUTTON, 9, 1)
        self.assertEqual(pad.step, 6)

    def test_plan_clamps_and_blocks_leaving_the_limit(self):
        angles = {n: 90 for n in range(1, 7)}
        limits = parse_limits(['2:40:100'])
        self.assertEqual(plan(angles, {2: 1}, 6, limits), {2: 96})
        self.assertEqual(plan(angles, {2: 1}, 20, limits), {2: 100})
        angles[2] = 100
        self.assertEqual(plan(angles, {2: 1}, 6, limits), {})
        angles[2] = 30  # outside the limit: may only move back in
        self.assertEqual(plan(angles, {2: -1}, 6, limits), {})
        self.assertEqual(plan(angles, {2: 1}, 6, limits), {2: 36})
        angles[5] = 270
        self.assertEqual(plan(angles, {5: 1}, 6, dict(RANGES)), {})

    def test_bad_limits_rejected(self):
        for bad in ('2:100:40', '9:0:10', '5:0:300', 'x'):
            with self.assertRaises(ValueError):
                parse_limits([bad])


if __name__ == '__main__':
    unittest.main()
