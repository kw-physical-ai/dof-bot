"""Dataset integrity tests without opening cameras or sending motor commands."""
import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np
from school_pdf import data_collect as module


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = patch.object(module, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        self.angles = dict(enumerate(module.HOME, 1))
        self.read = patch.object(module, 'read_angles', return_value=self.angles.copy())
        self.read.start()
        self.addCleanup(self.read.stop)
        self.collector = module.Collector()
        self.collector.homed = True
        self.pad = module.gp.Pad(stop=9, step_button=None, steps=(1,), phase_button=8)
        self.addCleanup(lambda: self.collector.finish('test complete'))

    def frames(self):
        for index in range(2):
            self.collector.frame(index, time.monotonic(), np.zeros((480, 640, 3), dtype=np.uint8))

    def test_dual_frames_match_csv_and_are_not_repeated(self):
        self.frames()
        self.collector.begin(None, self.pad)
        self.collector.save()
        self.collector.save()
        self.assertEqual(self.collector.count, 1)
        self.frames()
        self.collector.save()
        self.collector.finish('stop button')
        folder = self.collector.path
        with (folder / 'labels.csv').open() as source:
            rows = list(csv.reader(source))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][2:], [str(self.angles[i]) for i in range(1, 7)])
        for camera in ['cam1', 'cam2']:
            self.assertEqual(sorted(p.name for p in (folder / camera).glob('*.jpg')),
                             ['00001.jpg', '00002.jpg'])
            self.assertEqual(module.cv2.imread(str(folder / camera / '00001.jpg')).shape,
                             (244, 244, 3))
        self.assertEqual(json.loads((folder / 'meta.json').read_text())['frames'], 2)
        self.assertFalse(self.collector.active)

    def test_missing_or_stale_camera_prevents_session(self):
        with self.assertRaises(RuntimeError):
            self.collector.begin(None, self.pad)
        self.frames()
        self.collector.frames[1] = (0, self.collector.frames[1][1])
        with self.assertRaises(RuntimeError):
            self.collector.begin(None, self.pad)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_failed_image_write_does_not_append_label(self):
        self.frames()
        self.collector.begin(None, self.pad)
        with patch.object(module.cv2, 'imwrite', side_effect=[True, False]):
            with self.assertRaises(RuntimeError):
                self.collector.save()
        self.collector.finish('write error')
        with (self.collector.path / 'labels.csv').open() as source:
            self.assertEqual(len(list(csv.reader(source))), 1)
        self.assertEqual(self.collector.count, 0)

    def test_stop_does_not_rewrite_previous_metadata(self):
        self.frames()
        self.collector.begin(None, self.pad)
        self.collector.save()
        self.collector.finish('operator stop')
        before = (self.collector.path / 'meta.json').read_text()
        self.collector.finish('server shutdown')
        self.assertEqual((self.collector.path / 'meta.json').read_text(), before)

    def test_any_unavailable_joint_prevents_manual_and_home_writes(self):
        for servo in range(1,7):
            for home in (False,True):
                with self.subTest(servo=servo,home=home):
                    arm=Mock()
                    self.pad.ready=True
                    self.collector.angles=self.angles.copy()
                    with patch.object(module,'read_angles',side_effect=RuntimeError(f'Servo {servo} angle unavailable')):
                        with self.assertRaises(RuntimeError):
                            self.collector.move(arm,self.pad,None,home=home)
                    arm.Arm_serial_servo_write.assert_not_called()
                    arm.Arm_serial_servo_write6.assert_not_called()
                    self.assertFalse(self.collector.status['feedback_valid'])

    def test_stop_received_during_feedback_read_prevents_writes(self):
        arm=Mock();self.pad.ready=True
        def stop(*args):
            self.pad.stopped=True
            return False
        with patch.object(self.collector,'poll_input',side_effect=stop):
            with self.assertRaises(RuntimeError):
                self.collector.move(arm,self.pad,None,home=True)
        arm.Arm_serial_servo_write6.assert_not_called()

    def test_latest_input_after_read_is_used_and_target_committed_after_write(self):
        arm=Mock();self.pad.ready=True
        self.collector.angles=self.angles.copy()
        self.pad.axes={0:32767,1:0}
        def reverse(*args):
            self.pad.axes[0]=-32767
            return False
        with patch.object(self.collector,'poll_input',side_effect=reverse):
            self.collector.move(arm,self.pad,None)
        arm.Arm_serial_servo_write.assert_called_once_with(1,93,150)
        self.assertEqual(self.collector.angles[1],93)
        self.assertTrue(self.collector.status['feedback_valid'])

    def test_failed_write_does_not_update_saved_target(self):
        arm=Mock();arm.Arm_serial_servo_write.side_effect=OSError('bus failed')
        self.pad.ready=True;self.pad.axes={0:32767,1:0}
        self.collector.angles=self.angles.copy()
        with patch.object(self.collector,'poll_input',return_value=False):
            with self.assertRaises(OSError):self.collector.move(arm,self.pad,None)
        self.assertEqual(self.collector.angles,self.angles)

    def manual_move(self, measured, request):
        arm = Mock()
        self.pad.ready = True
        self.collector.angles = self.angles.copy()
        with patch.object(module, 'read_angles', return_value=measured), \
             patch.object(self.collector, 'poll_input', return_value=False), \
             patch.object(self.pad, 'request', return_value=request):
            self.collector.move(arm, self.pad, None)
        return arm

    def test_limit_blocks_only_outward_joint_and_allows_reverse(self):
        for servo in range(1, 7):
            low, high = module.LIMITS[servo]
            for angle, outward in ((low, -1), (high, 1)):
                with self.subTest(servo=servo, angle=angle):
                    other = 2 if servo == 1 else 1
                    self.angles[servo] = angle
                    arm = self.manual_move(self.angles.copy(), {servo: outward, other: 1})
                    arm.Arm_serial_servo_write.assert_called_once_with(other, self.angles[other]+3, 150)
                    arm = self.manual_move(self.angles.copy(), {servo: -outward})
                    arm.Arm_serial_servo_write.assert_called_once_with(servo, angle-outward*3, 150)
            self.angles[servo] = 90

    def test_one_missing_joint_does_not_block_healthy_joint(self):
        measured = self.angles.copy()
        measured[3] = None
        arm = self.manual_move(measured, {3: 1, 1: 1})
        arm.Arm_serial_servo_write.assert_called_once_with(1, 93, 150)
        self.assertEqual(self.collector.angles[3], self.angles[3])
        self.assertIsNone(self.collector.status['measured'][3])
        self.assertFalse(self.collector.status['feedback_valid'])

    def test_all_missing_joints_stop_without_writes(self):
        with self.assertRaises(RuntimeError):
            self.manual_move(dict.fromkeys(range(1, 7)), {1: 1})

    def test_fifth_joint_can_cross_180(self):
        self.angles[5] = 180
        arm = self.manual_move(self.angles.copy(), {5: 1})
        arm.Arm_serial_servo_write.assert_called_once_with(5, 183, 150)

    def test_home_pose_keeps_slow_two_second_move(self):
        arm=Mock();self.pad.ready=True
        self.collector.angles=self.angles.copy()
        with patch.object(self.collector,'poll_input',return_value=False):
            self.collector.move(arm,self.pad,None,home=True)
        arm.Arm_serial_servo_write6.assert_called_once_with(90,150,0,0,90,30,2000)

    def test_session_cannot_start_without_full_home_feedback(self):
        self.frames()
        measured=self.angles.copy();measured[1]=None
        with patch.object(module,'read_angles',return_value=measured):
            with self.assertRaises(RuntimeError):self.collector.begin(Mock(),self.pad)
        self.assertFalse(self.collector.active)
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_missing_initial_target_recovers_from_real_feedback(self):
        self.angles[1]=None
        measured=self.angles.copy();measured[1]=42
        arm=self.manual_move(measured,{1:1})
        arm.Arm_serial_servo_write.assert_called_once_with(1,45,150)

    def test_home_moves_only_joints_with_feedback(self):
        arm=Mock();self.pad.ready=True
        self.collector.angles=self.angles.copy()
        measured=self.angles.copy();measured[1]=None
        with patch.object(module,'read_angles',return_value=measured), \
             patch.object(self.collector,'poll_input',return_value=False):
            self.collector.move(arm,self.pad,None,home=True)
        arm.Arm_serial_servo_write6.assert_not_called()
        self.assertEqual([c.args[0] for c in arm.Arm_serial_servo_write.call_args_list],[2,3,4,5,6])

    def test_faster_step_clamps_to_limit_without_overshoot(self):
        self.angles[5]=269
        arm=self.manual_move(self.angles.copy(),{5:1})
        arm.Arm_serial_servo_write.assert_called_once_with(5,270,150)

    def test_startup_pose_moves_known_joints_to_configured_home(self):
        arm=Mock()
        with patch.object(self.collector.quit,'wait',return_value=False), \
             patch.object(module,'read_angles',side_effect=[self.angles.copy(),dict(enumerate(module.HOME,1))]):
            self.collector.initialize_pose(arm)
        arm.Arm_serial_servo_write6.assert_called_once_with(90,150,0,0,90,30,2000)
        self.assertTrue(self.collector.homed)

    def test_startup_no_feedback_sends_no_blind_home_commands(self):
        arm=Mock()
        with patch.object(module,'read_angles',return_value=dict.fromkeys(range(1,7))):
            with self.assertRaises(RuntimeError):self.collector.initialize_pose(arm)
        arm.Arm_serial_servo_write.assert_not_called()
        arm.Arm_serial_servo_write6.assert_not_called()

    def test_startup_requires_all_joints_before_home(self):
        arm=Mock();measured=self.angles.copy();measured[1]=None
        with patch.object(module,'read_angles',return_value=measured), \
             patch.object(self.collector.quit,'wait',return_value=False):
            with self.assertRaises(RuntimeError):self.collector.initialize_pose(arm)
        arm.Arm_serial_servo_write6.assert_not_called()
        self.assertFalse(self.collector.homed)

    def test_home_must_reach_target_before_collection(self):
        arm=Mock()
        self.angles[2]=120
        with patch.object(self.collector.quit,'wait',return_value=False), \
             patch.object(module,'read_angles',return_value=self.angles.copy()), \
             patch.object(module.time,'monotonic',side_effect=[0,6]):
            with self.assertRaisesRegex(RuntimeError,'도달 확인 실패'):
                self.collector.initialize_pose(arm)
        self.assertFalse(self.collector.homed)

    def test_boundary_feedback_uses_existing_narrow_recovery(self):
        measured=self.angles.copy();measured[3]=-1
        with patch.object(module,'read_angles',return_value=measured) as read:
            self.assertEqual(self.collector.checked_angles(Mock())[3],-1)
        read.assert_called_once_with(unittest.mock.ANY,recover=True,partial=False)

    def test_next_session_requires_home_after_stop(self):
        self.frames();self.collector.begin(Mock(),self.pad)
        self.collector.finish('stop')
        self.frames()
        with patch.object(self.collector,'initialize_pose',side_effect=RuntimeError('home failed')):
            with self.assertRaisesRegex(RuntimeError,'home failed'):
                self.collector.begin(Mock(),self.pad)
        self.assertFalse(self.collector.active)

    def test_vendor_startup_restores_stable_adjacent_boundary(self):
        arm=Mock();arm.Arm_ping_servo.return_value=0xDA
        measured=self.angles.copy();measured[3]=None
        with patch.object(module,'read_angles',side_effect=[measured,self.angles.copy()]), \
             patch.object(module,'outside_angle',return_value=-35), \
             patch.object(self.collector.quit,'wait',return_value=False):
            self.collector.initialize_pose(arm)
        arm.Arm_serial_servo_write6.assert_called_once_with(90,150,0,0,90,30,2000)
        self.assertTrue(self.collector.homed)

    def test_startup_recovery_rejects_unknown_far_and_nonadjacent_targets(self):
        for servo,estimate,ping in ((3,None,0xDA),(3,-46,0xDA),(3,-35,0),(1,-35,0xDA)):
            with self.subTest(servo=servo,estimate=estimate,ping=ping):
                arm=Mock();arm.Arm_ping_servo.return_value=ping
                measured=self.angles.copy();measured[servo]=None
                with patch.object(module,'read_angles',return_value=measured), \
                     patch.object(module,'outside_angle',return_value=estimate):
                    with self.assertRaises(RuntimeError):self.collector.initialize_pose(arm)
                arm.Arm_serial_servo_write6.assert_not_called()

    def test_negative_or_excessive_home_value_never_sends_command(self):
        for home in ([-1,150,0,0,90,30],[90,200,0,0,90,30]):
            arm=Mock()
            with patch.object(module,'HOME',home):
                with self.assertRaises(RuntimeError):self.collector.initialize_pose(arm)
            arm.Arm_serial_servo_write6.assert_not_called()

    def test_unrequested_boundary_does_not_delay_healthy_joint(self):
        measured=self.angles.copy();measured[3]=None
        with patch.object(module,'outside_angle') as extended:
            arm=self.manual_move(measured,{1:1})
        extended.assert_not_called()
        arm.Arm_serial_servo_write.assert_called_once_with(1,93,150)

    def test_requested_boundary_can_move_inward(self):
        measured=self.angles.copy();measured[3]=None
        with patch.object(module,'outside_angle',return_value=-1):
            arm=self.manual_move(measured,{3:1})
        arm.Arm_serial_servo_write.assert_called_once_with(3,3,150)

    def test_home_can_settle_after_first_read_without_resending(self):
        arm=Mock();off=self.angles.copy();off[1]=80
        with patch.object(module,'read_angles',side_effect=[off,off,self.angles.copy()]), \
             patch.object(self.collector.quit,'wait',return_value=False):
            self.collector.initialize_pose(arm)
        arm.Arm_serial_servo_write6.assert_called_once()
        self.assertTrue(self.collector.homed)

    def test_feedback_at_limit_blocks_outward_even_if_target_is_inside(self):
        measured = self.angles.copy()
        measured[1] = 180
        arm = self.manual_move(measured, {1: 1, 2: 1})
        arm.Arm_serial_servo_write.assert_called_once_with(2, 153, 150)


if __name__ == '__main__':
    unittest.main()
