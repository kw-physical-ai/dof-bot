import contextlib
import io
import os
import struct
import threading
import time
import unittest

from gamepad_control import (AXIS, BUTTON, INIT, RANGES, Pad, parse_limits, plan, run)


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
        try:
            with contextlib.redirect_stdout(out):
                run(read_fd,Pad(),arm,parse_limits([]),None)
        finally:
            thread.join()
            os.close(read_fd)
            os.close(write_fd)
        self.assertEqual(sum(s==3 and target>90 for s,target in arm.writes),1)
        self.assertTrue(any(s==6 for s,target in arm.writes))
        self.assertTrue(any(s==3 and target<90 for s,target in arm.writes))
        self.assertIn('reverse movement confirmed',out.getvalue())
        self.assertNotIn('Motion paused',out.getvalue())


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
