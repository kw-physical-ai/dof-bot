"""Exercise the real writer/collector/export boundary with fake motors only."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

import numpy as np

from school_pdf import data_collect as collector_module
from school_pdf.dataset import validate_episode, review_episode, export_dataset
from school_pdf.episode import FollowingGuard


class RecordingTests(unittest.TestCase):
    def test_real_collector_event_contract_round_trips_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            context=dict(task='paper',destination='paper_bin',object_id='paper-a',position_id='A1',
                         scene_id='scene-a',operator='test',camera_setup='fixed-v1',bin_layout='layout-v1')
            collector=collector_module.Collector(context=context)
            collector.task_config['mapping_confirmed']=True
            collector.homed=True
            arm=Mock();position=dict(enumerate(collector_module.HOME,1))
            arm.Arm_serial_servo_write.side_effect=lambda servo,target,duration: position.update({servo:target})
            pad=Mock(ready=True,stopped=False)
            def frames(color):
                for index in (0,1):
                    collector.frame(index,time.monotonic(),np.full((480,640,3),color+index,np.uint8))
            frames(20)
            with patch.object(collector_module,'ROOT',root), \
                 patch.object(collector_module,'read_angles',side_effect=lambda *a,**kw: position.copy()), \
                 patch.object(collector,'poll_input',return_value=False):
                collector.begin(arm,pad)
                for direction,color in ((1,50),(-1,90)):
                    time.sleep(.2)
                    frames(color);pad.ready=True;pad.request.return_value={1:direction}
                    collector.move(arm,pad,None)
                    collector.save()
                collector.finish('정지됨 · 저장 완료')
            report=validate_episode(collector.path)
            self.assertTrue(report['eligible'],report['errors'])
            self.assertEqual([s['actions'][0] for s in report['samples']],[1,-1])
            steps=[json.loads(row) for row in (collector.path/'steps.jsonl').read_text().splitlines()]
            for step in steps:
                self.assertLessEqual(step['feedback_end_ns'],step['observation_ns'])
                self.assertLessEqual(step['observation_ns'],step['writes'][0]['start_ns'])
            self.assertEqual(steps[0]['measured'][0],90)
            self.assertEqual(steps[0]['writes'][0]['target'],93)
            collector.input_mode='pc';collector.remote.client='review-client'
            server=ThreadingHTTPServer(('127.0.0.1',0),collector.handler())
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def post_review(episode_id):
                return urlopen(Request('http://127.0.0.1:'+str(server.server_port)+'/review',
                    data=json.dumps(dict(episode_id=episode_id,result='success')).encode(),
                    headers={'X-Control-Key':collector.key,'X-Gamepad-Client':'review-client'},method='POST'))
            try:
                with self.assertRaises(HTTPError) as error:post_review('old-session')
                self.assertEqual(error.exception.code,400)
                with post_review(collector.status['episode_id']) as response:
                    self.assertEqual(response.status,200)
            finally:
                server.shutdown();server.server_close();thread.join()
            manifest=export_dataset(root,root/'export',smoke_only=True)
            data=json.loads(manifest.read_text())
            self.assertTrue(data['smoke_only']);self.assertFalse(data['splits']['val'])
            self.assertEqual(len(data['splits']['train']),2)

    def test_no_progress_blocks_only_that_direction_and_waits_for_inflight(self):
        guard=FollowingGuard(attempts=3)
        guard.sent(1,1,72,150,now=0)
        self.assertEqual(guard.filter({1:72},{1:1},now=.1)[0],{})
        for moment in (.2,.4):
            allowed,issues=guard.filter({1:72,2:90},{1:1,2:1},now=moment)
            self.assertIn(1,allowed);self.assertFalse(issues)
            guard.sent(1,1,72,150,now=moment)
        allowed,issues=guard.filter({1:72,2:90},{1:1,2:1},now=.6)
        self.assertEqual(allowed,{2:1});self.assertEqual(issues,['servo_1_no_progress'])
        allowed,_=guard.filter({1:72},{1:-1},now=.8)
        self.assertEqual(allowed,{1:-1})

    def test_gripper_contact_is_not_automatically_declared_arm_failure(self):
        guard=FollowingGuard(attempts=1)
        guard.sent(6,1,100,150,now=0)
        allowed,issues=guard.filter({6:100,2:90},{6:1,2:1},now=.2)
        self.assertEqual(allowed,{2:1})
        self.assertEqual(issues,['gripper_contact_or_no_progress'])

    def test_vendor_container_app_detected_without_touching_it(self):
        with tempfile.TemporaryDirectory() as directory:
            process=Path(directory)/'123';process.mkdir()
            (process/'cmdline').write_bytes(b'python3\0/root/APP_DOFBOT/YahboomArm.pyc\0')
            with self.assertRaisesRegex(RuntimeError,'123'):
                collector_module.check_vendor_apps(Path(directory))

    def test_success_review_cannot_hide_actuator_nonprogress(self):
        with tempfile.TemporaryDirectory() as directory:
            context=dict(task='paper',destination='paper_bin',object_id='paper-a',position_id='A1',
                         scene_id='scene-a',operator='test',camera_setup='fixed-v1',bin_layout='layout-v1')
            collector=collector_module.Collector(context=context)
            collector.task_config['mapping_confirmed']=True;collector.homed=True
            arm=Mock();position=dict(enumerate(collector_module.HOME,1))
            def write(servo,target,duration):
                if servo != 1:position[servo]=target
            arm.Arm_serial_servo_write.side_effect=write
            pad=Mock(ready=True,stopped=False)
            with patch.object(collector_module,'ROOT',Path(directory)), \
                 patch.object(collector_module,'read_angles',side_effect=lambda *a,**kw: position.copy()), \
                 patch.object(collector,'poll_input',return_value=False):
                for camera in (0,1):collector.frame(camera,time.monotonic(),np.zeros((64,64,3),np.uint8))
                collector.begin(arm,pad)
                for i in range(4):
                    time.sleep(.2)
                    for camera in (0,1):collector.frame(camera,time.monotonic(),np.full((64,64,3),i*20,np.uint8))
                    pad.ready=True;pad.request.return_value={1:1,2:1}
                    collector.move(arm,pad,None);collector.save()
                collector.finish('정지됨 · 저장 완료')
            self.assertEqual(sum(c.args[0]==1 for c in arm.Arm_serial_servo_write.call_args_list),3)
            self.assertEqual(sum(c.args[0]==2 for c in arm.Arm_serial_servo_write.call_args_list),4)
            meta=json.loads((collector.path/'meta.json').read_text())
            self.assertIn('servo_1_no_progress',meta['safety_events'])
            review_episode(collector.path,'success','test')
            report=validate_episode(collector.path)
            self.assertFalse(report['eligible'])
            self.assertTrue(any('safety' in error for error in report['errors']))


if __name__=='__main__':
    unittest.main()
