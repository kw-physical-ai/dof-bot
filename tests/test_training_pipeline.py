"""Offline synthetic smoke tests; never import collector or robot SDK."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import runpy
import unittest
from unittest.mock import patch

TORCH_AVAILABLE = importlib.util.find_spec('torch') is not None
if TORCH_AVAILABLE:
    import cv2
    import numpy as np
    import torch
    from school_pdf.policy import Policy, CLASSES, LIMITS, resolve_device
    from school_pdf.train import canonical_hash, train_manifest, evaluate_checkpoint, load_checkpoint, ManifestDataset, action_metrics
    from school_pdf.preview import preview


@unittest.skipUnless(TORCH_AVAILABLE, 'Install requirements-train.txt in a training venv')
class TrainingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        torch.set_num_threads(1)

    def fixture(self, smoke=False, camera_mode='csi', steps=2, initial_idle_steps=0):
        from school_pdf.dataset import review_episode, export_dataset
        make_episode = runpy.run_path(str(Path(__file__).with_name('test_dataset_pipeline.py')))['make_episode']
        raw = self.root / f'raw-{camera_mode}-{smoke}'
        raw.mkdir()
        for task_index, task in enumerate(CLASSES):
            for index in range(1 if smoke else 2):
                episode = make_episode(raw, task, f'{task}-{index}', marker=task_index * 40 + index * 7, steps=steps)
                if initial_idle_steps:
                    events=[json.loads(line) for line in (episode/'steps.jsonl').read_text().splitlines()]
                    template=events[0]['writes'][0]
                    for step_index,event in enumerate(events):
                        active=step_index==initial_idle_steps
                        event['kind']='manual' if active else 'idle'
                        event['writes']=[dict(template,start_ns=event['observation_ns']+1_000_000,
                                               end_ns=event['observation_ns']+2_000_000)] if active else []
                    (episode/'steps.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events))
                review_episode(episode, 'success', 'synthetic-fixture')
        path = export_dataset(raw, self.root / f'export-{camera_mode}-{smoke}', camera_mode=camera_mode, smoke_only=smoke)
        return path, json.loads(path.read_text())

    def test_train_reload_evaluate_preview_and_binding(self):
        path, manifest = self.fixture()
        run = self.root / 'run'
        report = train_manifest(path, run, device='cpu', epochs=1, batch_size=3, image_size=32)
        self.assertTrue(np.isfinite(report['history'][0]['loss']))
        self.assertTrue((run / 'best.pt').is_file())
        metrics = evaluate_checkpoint(run / 'best.pt', path, device='cpu')
        self.assertEqual(metrics['samples'], 6)
        self.assertEqual(metrics['recognition']['initial_view_episodes'], 3)
        self.assertEqual(metrics['hold_all_active_joint_accuracy'], 0.)
        rows = preview(run / 'best.pt', path, self.root / 'preview.json', device='cpu')['rows']
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(target is None or 0 <= target <= LIMITS[i][1] for row in rows for i, target in enumerate(row['proposed_targets'])))
        auto = preview(run / 'best.pt', path, self.root / 'auto.json', device='cpu', task='auto')
        self.assertTrue(all(row['status'] == 'stop' for row in auto['rows']))
        manifest['gates']['max_camera_age_ms'] += 1
        altered = self.root / 'altered.json'; altered.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'exact training manifest'):
            evaluate_checkpoint(run / 'best.pt', altered, device='cpu')

    def test_smoke_cli_has_no_validation_claim(self):
        path, _ = self.fixture(smoke=True)
        run = self.root / 'smoke'
        for command in (
            [sys.executable, '-m', 'school_pdf.train', 'train', str(path), '--output', str(run), '--device', 'cpu', '--epochs', '1', '--batch-size', '3', '--image-size', '32'],
            [sys.executable, '-m', 'school_pdf.train', 'evaluate', str(run / 'last.pt'), str(path), '--split', 'train', '--device', 'cpu'],
            [sys.executable, '-m', 'school_pdf.preview', str(run / 'last.pt'), str(path), '--split', 'train', '--output', str(self.root / 'cli-preview.json'), '--device', 'cpu'],
        ):
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((run / 'best.pt').exists())
        self.assertIsNone(json.loads((run / 'metrics.json').read_text())['history'][0]['validation'])
        with self.assertRaises(ValueError):
            evaluate_checkpoint(run / 'last.pt', path, device='cpu')

    def test_initial_idle_preservation_and_balanced_action_metrics(self):
        path, manifest = self.fixture(smoke=True,steps=5,initial_idle_steps=3)
        active = next(s for s in manifest['splits']['train'] if any(s['actions']))
        initial = next(s for s in manifest['splits']['train'] if not any(s['actions']))
        other = dict(initial, classification_sample=False)
        manifest['splits']['train'] = [active] + [initial] * 4 + [other] * 20
        dataset = ManifestDataset(manifest, cap_idle=True)
        self.assertEqual(dataset.idle_cap['preserved_initial_idle_rows'], 4)
        self.assertEqual(dataset.idle_cap['other_idle_rows_after'], 1)
        target = np.array([0, 1, 2])
        perfect = action_metrics(target, target)
        hold = action_metrics(target, np.ones(3, dtype=int))
        self.assertEqual(perfect['confusion'], [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
        self.assertEqual(perfect['macro_f1'], 1.)
        self.assertLess(hold['macro_f1'], perfect['macro_f1'])

    def test_mutated_raw_images_rejected_before_training_and_evaluation(self):
        path, manifest = self.fixture()
        run = self.root / 'run'
        train_manifest(path, run, device='cpu', epochs=1, image_size=32)
        image = Path(manifest['splits']['train'][0]['images']['csi'])
        cv2.imwrite(str(image), np.zeros((12, 16, 3), dtype=np.uint8))
        with self.assertRaisesRegex(ValueError, 'Raw source changed'):
            evaluate_checkpoint(run / 'best.pt', path, device='cpu')
        with self.assertRaisesRegex(ValueError, 'Raw source changed'):
            train_manifest(path, self.root / 'changed', device='cpu', epochs=1)

    def test_auto_observes_then_latches_without_using_late_classifier(self):
        _, manifest = self.fixture(smoke=True, camera_mode='dual', steps=5, initial_idle_steps=3)
        episode_id = manifest['splits']['train'][0]['episode_id']
        manifest['splits']['train'] = [s for s in manifest['splits']['train'] if s['episode_id'] == episode_id]
        class ChangingClassifier:
            image_size = 32
            def __init__(self):
                self.calls, self.late_task_ids = 0, []
            def __call__(self, images, measured, task_ids):
                self.calls += 1
                classes = torch.tensor([[12., 0., 0.]]) if self.calls <= 3 else torch.tensor([[0., 0., 12.]])
                if self.calls > 3:
                    self.late_task_ids.append(int(task_ids[0]))
                return torch.zeros(1, 6, 3), classes
        model = ChangingClassifier()
        saved = {'policy': {'camera_mode': 'dual'}, 'task_config_hash': manifest['task_config_hash'],
                 'contract': manifest['contract'], 'classifier_supervised_rows': 3, 'manifest_fingerprint': canonical_hash(manifest)}
        with patch('school_pdf.preview.load_checkpoint', return_value=(model, saved, manifest, torch.device('cpu'))):
            result = preview('unused', 'unused', self.root / 'latched.json', split='train', task='auto', single_target=True)
        self.assertEqual([r['status'] for r in result['rows']], ['observe'] * 3 + ['proposal'] * 2)
        self.assertTrue(all(r['actions'] is None for r in result['rows'][:3]))
        self.assertTrue(all(r['task'] == 'paper' for r in result['rows'][3:]))
        self.assertEqual(model.late_task_ids, [0, 0])

    def test_dual_forward_csi_classifier_and_device_error(self):
        model = Policy('dual', image_size=32).eval()
        images = torch.rand(2, 2, 3, 32, 32)
        with torch.no_grad():
            actions, classes = model(images, torch.ones(2, 6) * 90, torch.tensor([0, 2]))
            images[:, 1] = 0
            _, changed = model(images, torch.ones(2, 6) * 90, torch.tensor([0, 2]))
        self.assertEqual(actions.shape, (2, 6, 3))
        self.assertTrue(torch.equal(classes, changed))
        with self.assertRaises(ValueError):
            resolve_device('imaginary')

    def test_leakage_and_unconfirmed_mapping_rejected(self):
        path, manifest = self.fixture()
        manifest['episodes'][-1]['object_id'] = manifest['episodes'][0]['object_id']
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'leakage'):
            train_manifest(path, self.root / 'bad', device='cpu', epochs=1)
        path, manifest = self.fixture(smoke=True)
        manifest['task_config']['mapping_confirmed'] = False
        manifest['task_config_hash'] = canonical_hash(manifest['task_config'])
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'unconfirmed'):
            train_manifest(path, self.root / 'bad', device='cpu', epochs=1)


if __name__ == '__main__':
    unittest.main()
