"""Causal dataset and review tests using tiny synthetic, hardware-free recordings."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import cv2
import numpy as np
from school_pdf import dataset


def make_episode(root, task='paper', object_id='object1', marker=1, steps=2):
    path = Path(root) / (task+'_'+object_id)
    path.mkdir()
    config = dict(version=1,layout_id='layout-v1',mapping_confirmed=True,
                  tasks={t:dict(destination=t+'_bin') for t in dataset.CLASSES})
    context = dict(task=task,destination=task+'_bin',object_id=object_id,position_id='p1',
                   scene_id='s1',operator='test',camera_setup='dual',bin_layout='layout-v1')
    meta = dict(schema_version=2,status='complete',episode_id=str(uuid.uuid4()),
                created_at='2026-01-01T00:00:00Z',end_at='2026-01-01T00:00:01Z',
                result='unreviewed',frames=steps,steps=steps,recording_context=context,
                task_config=config,task_config_hash=dataset.config_hash(config),
                limits=dataset.LIMITS,manual_step=3,manual_move_ms=150,safety_events=[])
    (path/'meta.json').write_text(json.dumps(meta))
    events = []
    for index in range(1,steps+1):
        observation = index*1_000_000_000
        image = np.full((12,16,3),marker+index,dtype=np.uint8)
        images = {}
        for camera, role in (('cam1','wrist'),('cam2','csi')):
            (path/camera).mkdir(exist_ok=True)
            (path/'events'/camera).mkdir(parents=True,exist_ok=True)
            cv2.imwrite(str(path/camera/f'{index:05d}.jpg'),image)
            name = f'events/{camera}/{index:06d}.jpg'
            cv2.imwrite(str(path/name),image)
            images[role] = dict(path=name,receive_ns=observation-20_000_000,source_id=index)
        events.append(dict(id=index,kind='manual' if index==1 else 'idle',observation_ns=observation,
            feedback_start_ns=observation-10_000_000,feedback_end_ns=observation-1_000_000,
            measured=[90]*6,sources=['sdk']*6,images=images,complete=True,blocked={},
            writes=[dict(servo=1,direction=1,target=93,duration_ms=150,start_ns=observation+1_000_000,
                         end_ns=observation+2_000_000,status='sent')] if index==1 else []))
    (path/'steps.jsonl').write_text(''.join(json.dumps(s)+'\n' for s in events))
    with (path/'labels.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['frame_id','timestamp']+[f'm{i}' for i in range(1,7)])
        for i in range(1,steps+1):writer.writerow([i,'000000.000']+[93]+[90]*5)
    with (path/'frames.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['frame_id','cam1_receive_monotonic','cam2_receive_monotonic'])
        for i in range(1,steps+1):writer.writerow([i,i-.02,i-.02])
    return path


def change_step(path, callback):
    steps = [json.loads(s) for s in (path/'steps.jsonl').read_text().splitlines()]
    callback(steps)
    (path/'steps.jsonl').write_text(''.join(json.dumps(s)+'\n' for s in steps))


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'raw'
        self.root.mkdir()

    def test_valid_categorical_actions_and_idle(self):
        path=make_episode(self.root)
        result=dataset.validate_episode(path,'dual')
        self.assertTrue(result['eligible'],result['errors'])
        self.assertEqual(result['samples'][0]['actions'],[1,0,0,0,0,0])
        self.assertEqual(result['samples'][1]['actions'],[0]*6)
        self.assertEqual([s['classification_sample'] for s in result['samples']],[True,False])

    def test_causal_gates_and_corruption_cannot_be_reviewed_away(self):
        mutations = [
            lambda s:s[0]['writes'][0].update(start_ns=s[0]['observation_ns']-1),
            lambda s:s[0]['images']['csi'].update(receive_ns=1),
            lambda s:s[0]['writes'][0].update(target=96),
            lambda s:s[0]['writes'][0].update(status='error'),
            lambda s:s[0]['measured'].__setitem__(2,None),
            lambda s:s[0].update(complete=False),
            lambda s:s[1]['images']['csi'].update(source_id=0),
        ]
        for index,mutate in enumerate(mutations):
            with self.subTest(index=index):
                path=make_episode(self.root,object_id=str(index))
                change_step(path,mutate)
                dataset.review_episode(path,'success','operator')
                self.assertFalse(dataset.validate_episode(path)['eligible'])
                with self.assertRaisesRegex(ValueError,'no eligible'):
                    dataset.export_dataset(path,Path(self.temp.name)/f'out{index}',smoke_only=True)

    def test_review_digest_detects_modified_images_and_labels(self):
        path=make_episode(self.root)
        dataset.review_episode(path,'success','operator')
        old=json.loads((path/'review.json').read_text())['source_digest']
        self.assertEqual(old,dataset.validate_episode(path)['digest'])
        image=path/'events/cam2/000001.jpg'
        cv2.imwrite(str(image),np.zeros((12,16,3),np.uint8))
        with self.assertRaisesRegex(ValueError,'no eligible'):
            dataset.export_dataset(path,Path(self.temp.name)/'out',smoke_only=True)
        self.assertNotEqual(old,dataset.validate_episode(path)['digest'])

    def test_compatibility_images_decoded_even_if_unused_by_model(self):
        path=make_episode(self.root)
        (path/'cam1/00001.jpg').write_bytes(b'bad jpeg')
        self.assertFalse(dataset.validate_episode(path)['eligible'])

    def test_legacy_review_allowed_but_training_excluded(self):
        path=self.root/'legacy';path.mkdir()
        (path/'meta.json').write_text('{"frames": 1}')
        review=dataset.review_episode(path,'success','operator',task='paper',destination='paper_bin')
        self.assertEqual(review['result'],'success')
        self.assertTrue(dataset.validate_episode(path)['legacy'])
        with self.assertRaises(ValueError):dataset.export_dataset(path,Path(self.temp.name)/'out',smoke_only=True)

    def test_mapping_and_context_checks(self):
        path=make_episode(self.root)
        with self.assertRaisesRegex(ValueError,'context'):
            dataset.review_episode(path,'success','operator',task='vinyl')
        meta=json.loads((path/'meta.json').read_text())
        meta['task_config']['mapping_confirmed']=False
        meta['task_config_hash']=dataset.config_hash(meta['task_config'])
        (path/'meta.json').write_text(json.dumps(meta))
        self.assertFalse(dataset.validate_episode(path)['eligible'])

    def test_smoke_is_train_only_and_output_refuses_overwrite(self):
        path=make_episode(self.root)
        dataset.review_episode(path,'success','operator')
        output=Path(self.temp.name)/'out'
        manifest=json.loads(dataset.export_dataset(self.root,output,smoke_only=True).read_text())
        self.assertTrue(manifest['smoke_only'])
        self.assertEqual(manifest['splits']['val'],[])
        self.assertEqual(len(manifest['splits']['train']),2)
        with self.assertRaisesRegex(ValueError,'absent or empty'):dataset.export_dataset(self.root,output,smoke_only=True)
        with self.assertRaisesRegex(ValueError,'two independent'):dataset.export_dataset(self.root,Path(self.temp.name)/'real')

    def test_deterministic_three_task_object_split_and_duplicate_grouping(self):
        # Every task's independent objects; an extra same-content recording is bound
        # to its source despite a different object identity.
        for task_index,task in enumerate(dataset.CLASSES):
            for index in range(3):
                path=make_episode(self.root,task,f'{task}{index}',marker=task_index*30+index*3)
                dataset.review_episode(path,'success','operator')
        duplicate=make_episode(self.root,'paper','duplicate',marker=0)
        dataset.review_episode(duplicate,'success','operator')
        first=json.loads(dataset.export_dataset(self.root,Path(self.temp.name)/'a').read_text())
        second=json.loads(dataset.export_dataset(self.root,Path(self.temp.name)/'b').read_text())
        self.assertEqual(first['splits'],second['splits'])
        for split in ('train','val'):
            self.assertEqual({s['task'] for s in first['splits'][split]},set(dataset.CLASSES))
        assignment={e['object_id']:e['split'] for e in first['episodes']}
        self.assertEqual(assignment['paper0'],assignment['duplicate'])
        self.assertFalse({e['object_id'] for e in first['episodes'] if e['split']=='train'} &
                         {e['object_id'] for e in first['episodes'] if e['split']=='val'})



    def test_repeated_frames_drop_samples_but_still_validate_all_writes(self):
        path=make_episode(self.root)
        change_step(path,lambda s:s[1]['images']['csi'].update(source_id=1))
        report=dataset.validate_episode(path)
        self.assertTrue(report['eligible'],report['errors'])
        self.assertEqual(len(report['samples']),1)
        self.assertEqual(report['counts']['rejected_samples'],1)
        self.assertIn('dropped sample',str(report['warnings']))
        # A new wrist frame makes dual observation a new pair even with CSI held.
        report=dataset.validate_episode(path,'dual')
        self.assertTrue(report['eligible'],report['errors'])
        self.assertEqual(len(report['samples']),2)
        def bad_writes(steps):
            steps[1].update(kind='manual',writes=[dict(steps[0]['writes'][0],start_ns=1,end_ns=2)])
        change_step(path,bad_writes)
        report=dataset.validate_episode(path)
        self.assertFalse(report['eligible'])
        self.assertIn('write occurs before',str(report['errors']))

    def test_arbitrary_destinations_need_distinct_names_and_exact_context(self):
        path=make_episode(self.root)
        meta=json.loads((path/'meta.json').read_text())
        names={'paper':'left blue tray','plastic':'가운데 빨간 통','vinyl':'right clear basket'}
        for task,name in names.items():
            meta['task_config']['tasks'][task]['destination']=name
        meta['recording_context']['destination']=names['paper']
        meta['task_config_hash']=dataset.config_hash(meta['task_config'])
        (path/'meta.json').write_text(json.dumps(meta))
        self.assertTrue(dataset.validate_episode(path)['eligible'])
        dataset.review_episode(path,'success','operator')
        manifest=json.loads(dataset.export_dataset(path,Path(self.temp.name)/'named',smoke_only=True).read_text())
        self.assertEqual(manifest['task_config']['tasks']['paper']['destination'],'left blue tray')
        meta['recording_context']['destination']='paper_bin'
        (path/'meta.json').write_text(json.dumps(meta))
        self.assertFalse(dataset.validate_episode(path)['eligible'])
        for invalid in ('', '   ', names['paper'], ' '+names['paper']+' '):
            with self.subTest(invalid=invalid):
                meta['task_config']['tasks']['vinyl']['destination']=invalid
                meta['task_config_hash']=dataset.config_hash(meta['task_config'])
                (path/'meta.json').write_text(json.dumps(meta))
                self.assertFalse(dataset.validate_episode(path)['eligible'])

    def test_legacy_inspection_decodes_counts_duration_and_preserves_exclusion(self):
        path=make_episode(self.root)
        meta=json.loads((path/'meta.json').read_text())
        meta.pop('schema_version')
        (path/'meta.json').write_text(json.dumps(meta))
        report=dataset.validate_episode(path)
        self.assertTrue(report['legacy'])
        self.assertFalse(report['eligible'])
        self.assertTrue(report['compatibility_integrity'])
        self.assertEqual(report['counts']['frames'],2)
        self.assertEqual(report['counts']['timing_rows'],2)
        self.assertEqual(report['counts']['images'],{'cam1':2,'cam2':2})
        self.assertAlmostEqual(report['duration_seconds'],1.0)
        self.assertEqual(report['source_observations']['cameras']['cam1']['decoded_images'],2)
        self.assertEqual(report['source_observations']['command_target_ranges'][0],[93.0,93.0])
        self.assertEqual(report['samples'],[])
        dataset.review_episode(path,'success','operator',task='paper',destination='paper_bin')
        report=dataset.validate_episode(path)
        self.assertTrue(report['source_observations']['operator_annotation']['digest_matches'])
        (path/'cam2/00001.jpg').write_bytes(b'broken jpeg')
        (path/'cam1/00002.jpg').unlink()
        report=dataset.validate_episode(path)
        self.assertFalse(report['compatibility_integrity'])
        self.assertIn('undecodable',str(report['errors']))
        self.assertIn('image names/count',str(report['errors']))
        self.assertFalse(report['source_observations']['operator_annotation']['digest_matches'])
        self.assertEqual(report['source_observations']['cameras']['cam2']['decoded_images'],1)
        self.assertFalse(report['eligible'])

    def test_path_traversal_and_missing_six_state_rejected(self):
        path=make_episode(self.root)
        change_step(path,lambda s:s[0]['images']['csi'].update(path='../external.jpg'))
        self.assertIn('escapes',str(dataset.validate_episode(path)['errors']))

    def test_idle_only_success_is_not_a_sorting_demonstration(self):
        path=make_episode(self.root)
        change_step(path,lambda steps:[step.update(kind='idle',writes=[]) for step in steps])
        dataset.review_episode(path,'success','operator')
        self.assertIn('no actual moving',str(dataset.validate_episode(path)['errors']))


if __name__=='__main__':unittest.main()
