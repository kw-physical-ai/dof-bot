import unittest
import os
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from arm_control import exclusive_arm, read_angles
from joystick_control import AXIS, BUTTON, INIT, Controls, Jogger, drain


class FakeArm:
    def __init__(self):
        self.angles = {n: 90 for n in range(1, 7)}
        self.writes = []
        self.follow = True

    def Arm_serial_servo_read(self, servo):
        return self.angles[servo]

    def Arm_serial_servo_write(self, servo, target, duration):
        self.writes.append((servo, target, duration))
        if self.follow:
            self.angles[servo] = target


class JoystickTests(unittest.TestCase):
    def tick(self, jogger, now, servo, direction):
        jogger.clock = lambda: now
        jogger.tick(servo, direction)

    def neutral(self):
        c = Controls()
        c.event(INIT | AXIS, 0, 0)
        c.event(INIT | BUTTON, 6, 0)
        c.settle()
        return c

    def test_starting_held_does_not_arm(self):
        c = Controls()
        c.event(INIT | AXIS, 0, 32767)
        c.event(INIT | BUTTON, 6, 1)
        c.settle()
        self.assertEqual(c.request(), 0)
        c.event(AXIS, 0, 0)
        c.event(BUTTON, 6, 0)
        c.settle()
        c.event(BUTTON, 6, 1)
        c.event(AXIS, 0, 32767)
        self.assertEqual(c.request(), 1)

    def test_release_stop_and_queue_reset_disarm(self):
        for event in ((BUTTON, 6, 0), (BUTTON, 7, 1), (INIT | AXIS, 1, 0)):
            c = self.neutral()
            c.event(BUTTON, 6, 1)
            c.event(AXIS, 0, -32767)
            self.assertEqual(c.request(), -1)
            c.event(*event)
            c.settle()
            self.assertEqual(c.request(), 0)

    def test_cannot_change_joint_while_enabled(self):
        c = self.neutral()
        c.event(BUTTON, 3, 1)
        self.assertEqual(c.servo, 4)
        c.event(BUTTON, 6, 1)
        c.event(BUTTON, 1, 1)
        self.assertEqual(c.servo, 4)

    def test_deflected_stick_cannot_arm(self):
        c = self.neutral()
        c.event(AXIS, 0, 32767)
        c.event(BUTTON, 6, 1)
        c.settle()
        self.assertEqual(c.request(), 0)

    def test_no_write_at_start_or_release(self):
        arm = FakeArm()
        j = Jogger(arm)
        self.tick(j, 1, 1, 0)
        self.assertEqual(arm.writes, [])
        self.tick(j, 2, 1, 1)
        self.tick(j, 2.1, 1, 1)
        self.tick(j, 3, 1, 0)
        self.assertEqual(arm.writes, [(1, 91, 250)])

    def test_limits_and_stall(self):
        arm = FakeArm()
        arm.angles[1] = 180
        j = Jogger(arm)
        self.tick(j, 1, 1, 1)
        self.assertEqual(arm.writes, [])
        arm.angles[5] = 270
        self.tick(j, 2, 5, 1)
        self.assertEqual(arm.writes, [])
        arm.follow = False
        for t in range(3):
            self.tick(j, t + 3, 2, 1)
        with self.assertRaises(RuntimeError):
            self.tick(j, 7, 2, 1)
        self.assertEqual(len(arm.writes), 3)

    def test_missing_or_invalid_feedback_blocks_writes(self):
        for invalid in (None, True, float('nan'), float('inf'), -1, 181):
            arm = FakeArm()
            arm.angles[6] = invalid
            with self.assertRaises(RuntimeError):
                read_angles(arm)
            self.assertEqual(arm.writes, [])
        arm = FakeArm()
        j = Jogger(arm)
        self.tick(j, 1, 1, 1)
        arm.angles[6] = None
        with self.assertRaises(RuntimeError):
            self.tick(j, 2, 1, 1)
        self.assertEqual(len(arm.writes), 1)

    def test_step_button_cycles_only_when_idle(self):
        c = Controls(step_button=8, steps=(1, 3, 6))
        for event in ((INIT | AXIS, 0, 0), (INIT | BUTTON, 6, 0)):
            c.event(*event)
        c.settle()
        self.assertEqual(c.step, 1)
        c.event(BUTTON, 8, 1)
        c.event(BUTTON, 8, 0)
        self.assertEqual(c.step, 3)
        c.event(BUTTON, 6, 1)
        c.event(BUTTON, 8, 1)
        self.assertEqual(c.step, 3)

    def test_larger_step_moves_more_with_proportional_time(self):
        arm = FakeArm()
        j = Jogger(arm)
        j.clock = lambda: 1
        j.tick(1, 1, step=6)
        self.assertEqual(arm.writes, [(1, 96, 600)])
        j.clock = lambda: 1.2
        j.tick(1, 1, step=6)
        self.assertEqual(len(arm.writes), 1)
        j.clock = lambda: 1.8
        j.tick(1, 1, step=6)
        self.assertEqual(arm.writes[-1], (1, 102, 600))

    def test_step_is_clamped_at_range_end(self):
        arm = FakeArm()
        arm.angles[1] = 178
        j = Jogger(arm)
        self.tick(j, 1, 1, 0)
        j.clock = lambda: 2
        j.tick(1, 1, step=6)
        self.assertEqual(arm.writes, [(1, 180, 250)])

    def test_transient_missing_read_is_retried_but_dead_servo_still_stops(self):
        arm = FakeArm()
        original = arm.Arm_serial_servo_read
        calls = {'n': 0}

        def flaky(servo):
            if servo == 6:
                calls['n'] += 1
                if calls['n'] == 1:
                    return None
            return original(servo)
        arm.Arm_serial_servo_read = flaky
        self.assertEqual(read_angles(arm)[6], 90)
        arm.Arm_serial_servo_read = lambda servo: None if servo == 1 else original(servo)
        with self.assertRaises(RuntimeError):
            read_angles(arm)

    def test_disconnected_input_raises(self):
        fd, writer = os.pipe()
        os.close(writer)
        try:
            with self.assertRaises(RuntimeError):
                drain(fd, self.neutral(), False)
        finally:
            os.close(fd)

    def test_opposite_motion_stops_even_within_tolerance(self):
        arm = FakeArm()
        j = Jogger(arm)
        self.tick(j, 1, 1, 1)
        arm.angles[1] = 89
        with self.assertRaises(RuntimeError):
            self.tick(j, 2, 1, 1)
        self.assertEqual(len(arm.writes), 1)

    def test_release_received_during_reads_prevents_write(self):
        arm = FakeArm()
        j = Jogger(arm)
        j.tick(1, 1, refresh=lambda: (1, 0))
        self.assertEqual(arm.writes, [])

    def test_command_clock_is_recorded_after_slow_reads(self):
        arm = FakeArm()
        now = [1.0]
        j = Jogger(arm, clock=lambda: now[0])
        original_read = arm.Arm_serial_servo_read
        def slow_read(servo):
            now[0] += 0.1
            return original_read(servo)
        arm.Arm_serial_servo_read = slow_read
        j.tick(1, 1)
        self.assertAlmostEqual(j.last_write, 1.6)
        now[0] = 1.7
        j.tick(1, 1)
        self.assertEqual(len(arm.writes), 1)

    def test_ownership_failure_and_double_start_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('arm_control.Path.home', return_value=Path(directory)):
                for result in (CompletedProcess([], 0, '1234', ''),
                               CompletedProcess([], 1, '', 'sudo: password required')):
                    with patch('arm_control.subprocess.run', return_value=result):
                        with self.assertRaises(RuntimeError):
                            with exclusive_arm():
                                self.fail('ownership failure must prevent entry')
                with patch('arm_control.subprocess.run',
                           return_value=CompletedProcess([], 1, '', '')), \
                        patch('arm_control.check_i2c_device'):
                    with exclusive_arm():
                        with self.assertRaises(RuntimeError):
                            with exclusive_arm():
                                self.fail('second process must not enter')


if __name__ == '__main__':
    unittest.main()
