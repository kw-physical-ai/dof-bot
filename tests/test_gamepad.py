import contextlib
import io
import os
import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch

from gamepad_control import (AXIS, BUTTON, INIT, RANGES, Pad, main, parse_limits, plan, run)


class StuckArm:
    """Reads fine but never follows a command."""

    def __init__(self):
        self.writes = []

    def Arm_serial_servo_read(self, servo):
        return 90

    def Arm_serial_servo_write(self, servo, target, duration):
        self.writes.append((servo, target))


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

    def test_phase_button_ignores_initial_and_repeated_held_events(self):
        pad = self.neutral(step_button=None, phase_button=9)
        pad.event(INIT | BUTTON, 9, 1)
        self.assertFalse(pad.phase_requested)
        pad.event(BUTTON, 9, 0)
        pad.event(BUTTON, 9, 1)
        self.assertTrue(pad.phase_requested)
        pad.phase_requested = False
        pad.event(BUTTON, 9, 1)
        self.assertFalse(pad.phase_requested)
        self.assertEqual(pad.step, 3)

    def test_phase_marker_reads_feedback_without_motor_writes(self):
        pad = self.neutral(step_button=None, phase_button=9)
        rec, arm = Mock(), StuckArm()
        calls = iter(['mark', 'stop'])
        def inputs(fd, current):
            event = next(calls, 'stop')
            if event == 'mark':
                current.event(BUTTON, 9, 1)
            elif rec.mark_grasp_complete.called:
                current.stopped = True
        with patch('gamepad_control.select.select'), patch('gamepad_control.drain', side_effect=inputs):
            run(0, pad, arm, parse_limits([]), None, recorder=rec)
        rec.mark_grasp_complete.assert_called_once()
        self.assertEqual(arm.writes, [])

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

    def test_startup_motion_requires_explicit_alignment_option(self):
        def ioctl(fd, command, values, mutate):
            values[0] = 2 if command == 0x80016a11 else 12
        for options, should_align in [([], False), (['--align-start'], True), (['--no-align'], False),
                                     (['--start', '1:90', '--start', '2:100'], True)]:
            with self.subTest(options=options), \
                    patch('sys.argv', ['gamepad_control.py', '--usb-only', '--stream', '0'] + options), \
                    patch('gamepad_control.os.open', return_value=123), \
                    patch('gamepad_control.os.close'), patch('gamepad_control.fcntl.ioctl', side_effect=ioctl), \
                    patch('gamepad_control.signal.signal'), \
                    patch('gamepad_control.exclusive_arm', return_value=contextlib.nullcontext()), \
                    patch('gamepad_control.open_arm'), \
                    patch('gamepad_control.align_start', return_value=True) as alignment, \
                    patch('gamepad_control.run') as control:
                self.assertEqual(main(), 0)
                self.assertEqual(alignment.called, should_align)
                self.assertEqual(control.call_args.kwargs['speed'], 40)

    def test_fast_repeat_waits_for_feedback_and_reuses_the_read(self):
        pad, arm = self.neutral(), StuckArm()
        angles = {s: 90 for s in RANGES}
        clock, writes = [0.0], []
        def write(s, target, duration):
            writes.append((clock[0], duration))
            angles[s] = target
        arm.Arm_serial_servo_write = write
        def select_tick(*args):
            clock[0] += .02
        def inputs(fd, current):
            if len(writes) >= 2:
                current.stopped = True
            else:
                current.event(AXIS, 0, -32767)
        with patch('gamepad_control.time.monotonic', side_effect=lambda: clock[0]), \
                patch('gamepad_control.select.select', side_effect=select_tick), \
                patch('gamepad_control.drain', side_effect=inputs), \
                patch('gamepad_control.read_angles', side_effect=lambda *a, **kw: dict(angles)) as reads:
            run(0, pad, arm, parse_limits([]), None)
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][1], 80)
        elapsed = writes[1][0] - writes[0][0]
        self.assertGreaterEqual(elapsed, .12 - 1e-6)
        self.assertLess(elapsed, .2)
        self.assertEqual(reads.call_count, 3)  # initial, first command, first feedback

    def test_delayed_shoulder_feedback_is_rechecked_without_resending(self):
        pad, arm = self.neutral(), StuckArm()
        clock, writes = [0.0], []
        def write(s, target, duration):
            writes.append((clock[0], target))
        arm.Arm_serial_servo_write = write
        def read(*args, **kwargs):
            q = {s: 90 for s in RANGES}
            if writes and clock[0] >= writes[0][0] + .22:
                q[2] = writes[0][1]
            return q
        def inputs(fd, current):
            if len(writes) >= 2:
                current.stopped = True
            else:
                current.event(AXIS, 1, 32767)
        def tick(*args):
            clock[0] += .02
        out = io.StringIO()
        with patch('gamepad_control.time.monotonic', side_effect=lambda: clock[0]), \
                patch('gamepad_control.select.select', side_effect=tick), \
                patch('gamepad_control.drain', side_effect=inputs), \
                patch('gamepad_control.read_angles', side_effect=read), contextlib.redirect_stdout(out):
            run(0, pad, arm, parse_limits([]), None)
        self.assertEqual([target for _, target in writes], [93, 96])
        self.assertGreaterEqual(writes[1][0] - writes[0][0], .22)
        self.assertNotIn('blocked', out.getvalue())

    def test_both_direction_stall_can_be_retried_after_neutral(self):
        pad, arm = self.neutral(), StuckArm()
        clock, writes = [0.0], []
        q = {s: 90 for s in RANGES}
        def write(s, target, duration):
            writes.append((clock[0], target))
            if clock[0] >= 1.4:
                q[s] = target
        arm.Arm_serial_servo_write = write
        def inputs(fd, current):
            current.event(AXIS, 1, 32767 if clock[0] < .6 or clock[0] >= 1.4
                          else -32767 if clock[0] < 1.2 else 0)
            if clock[0] >= 1.8:
                current.stopped = True
        def tick(*args):
            clock[0] += .02
        out = io.StringIO()
        with patch('gamepad_control.time.monotonic', side_effect=lambda: clock[0]), \
                patch('gamepad_control.select.select', side_effect=tick), \
                patch('gamepad_control.drain', side_effect=inputs), \
                patch('gamepad_control.read_angles', side_effect=lambda *a, **kw: dict(q)), \
                contextlib.redirect_stdout(out):
            run(0, pad, arm, parse_limits([]), None)
        self.assertIn('both-direction block cleared', out.getvalue())
        self.assertTrue(any(at >= 1.4 for at, _ in writes))
        self.assertGreater(q[2], 90)

    def test_all_feedback_missing_at_start_is_an_explicit_error(self):
        arm = StuckArm()
        with patch('gamepad_control.read_angles', return_value={s: None for s in RANGES}):
            with self.assertRaisesRegex(RuntimeError, 'All 6 servos unreadable'):
                run(0, self.neutral(), arm, parse_limits([]), None)
        self.assertEqual(arm.writes, [])

    def test_idle_refresh_recovers_unreadable_joint_without_motion(self):
        pad, arm, rec = self.neutral(), StuckArm(), Mock()
        before = {s: 90 for s in RANGES}
        before[4] = None
        after = {s: 90 for s in RANGES}
        clock = [0]
        def now():
            clock[0] += .6
            return clock[0]
        def inputs(fd, current):
            if rec.state.call_count >= 2:
                current.stopped = True
        with patch('gamepad_control.time.monotonic', side_effect=now), \
                patch('gamepad_control.drain', side_effect=inputs), \
                patch('gamepad_control.select.select'), \
                patch('gamepad_control.read_angles', side_effect=[before, after]):
            run(0, pad, arm, parse_limits([]), None, recorder=rec)
        self.assertEqual(rec.state.call_args.args[0][4], 90)
        self.assertEqual(arm.writes, [])

    @unittest.skipIf(os.name == 'nt', 'select() on a pipe needs Linux')
    def test_failed_direction_blocks_repeated_writes_without_exiting(self):
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)

        def send(kind, number, value):
            os.write(write_fd, struct.pack('=IhBB', 0, value, kind, number))

        def feeder():
            send(INIT | AXIS, 0, 0)
            send(INIT | AXIS, 1, 0)
            time.sleep(0.3)
            send(BUTTON, 6, 1)      # hold L2: servo 3 +, but the arm never follows
            time.sleep(2.5)
            send(BUTTON, 8, 1)      # SELECT: the only thing that ends the run

        thread = threading.Thread(target=feeder)
        arm, out = StuckArm(), io.StringIO()
        thread.start()
        try:
            with contextlib.redirect_stdout(out):
                run(read_fd, Pad(), arm, dict(RANGES), None)   # must return, not raise
        finally:
            thread.join()
            os.close(read_fd)
            os.close(write_fd)
        self.assertIn('S3 + blocked', out.getvalue())
        self.assertNotIn('Motion paused', out.getvalue())
        self.assertIn('Stop button', out.getvalue())
        # A 3-degree command the arm ignores is caught at the first check; after
        # that nothing more is written although the key stays held.
        self.assertEqual(len(arm.writes), 1)


    def test_reverse_and_other_joint_work_without_neutral_reset(self):
        class EndStopArm:
            def __init__(self):
                self.angles = {s:90 for s in RANGES}
                self.writes = []
            def Arm_serial_servo_read(self, servo):
                return self.angles[servo]
            def Arm_serial_servo_write(self, servo, target, duration):
                self.writes.append((servo, target))
                if servo != 3 or target < self.angles[servo]:
                    self.angles[servo] = target

        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        def send(kind, number, value):
            os.write(write_fd, struct.pack('=IhBB',0,value,kind,number))
        def feeder():
            send(INIT | AXIS,0,0)
            send(INIT | AXIS,1,0)
            time.sleep(.1)
            send(BUTTON,6,1)  # S3+ meets a simulated physical stop
            time.sleep(.8)   # allow readback to block that direction
            send(BUTTON,0,1)  # S6+ must work while S3+ remains held
            time.sleep(.5)
            send(BUTTON,6,0)
            send(BUTTON,4,1)  # reverse S3 without releasing S6 / resetting pad
            time.sleep(1.2)  # pending S6 tick, reverse command, then readback
            send(BUTTON,8,1)
        arm, out = EndStopArm(), io.StringIO()
        thread=threading.Thread(target=feeder)
        thread.start()
        recorder = Mock()
        try:
            with contextlib.redirect_stdout(out):
                run(read_fd,Pad(),arm,parse_limits([]),None,recorder=recorder)
        finally:
            thread.join()
            os.close(read_fd)
            os.close(write_fd)
        self.assertEqual(sum(s==3 and target>90 for s,target in arm.writes),1)
        self.assertTrue(any(s==6 for s,target in arm.writes))
        self.assertTrue(any(s==3 and target<90 for s,target in arm.writes))
        self.assertIn('reverse movement confirmed',out.getvalue())
        self.assertNotIn('Motion paused',out.getvalue())
        self.assertEqual(recorder.action.call_count, len(arm.writes))
        self.assertGreater(recorder.state.call_count, 1)


    def test_unknown_joint_at_start_does_not_disable_known_joint(self):
        class PartialArm:
            def __init__(self):
                self.writes=[]
                self.angles={s:90 for s in RANGES}
            def Arm_serial_servo_read(self, servo):
                return None if servo==1 else self.angles[servo]
            def Arm_serial_servo_write(self, servo,target,duration):
                self.writes.append((servo,target))
                self.angles[servo]=target
        read_fd,write_fd=os.pipe()
        os.set_blocking(read_fd,False)
        def send(kind,number,value):
            os.write(write_fd,struct.pack('=IhBB',0,value,kind,number))
        def feeder():
            send(INIT|AXIS,0,0)
            send(INIT|AXIS,1,0)
            time.sleep(.3)
            send(AXIS,0,-32767)  # request unavailable base too
            send(BUTTON,0,1)    # readable gripper should still move
            time.sleep(1.3)
            send(BUTTON,8,1)
        arm,out=PartialArm(),io.StringIO()
        thread=threading.Thread(target=feeder)
        thread.start()
        try:
            with contextlib.redirect_stdout(out):
                run(read_fd,Pad(),arm,parse_limits([]),None)
        finally:
            thread.join()
            os.close(read_fd)
            os.close(write_fd)
        self.assertTrue(arm.writes)
        self.assertTrue(all(s==6 for s,t in arm.writes))
        self.assertIn('only this joint is disabled',out.getvalue())


if __name__ == '__main__':
    unittest.main()
