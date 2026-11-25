"""Offline, manifest-bound training and evaluation for the sorting BC baseline."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
from .policy import Policy, CLASSES, LIMITS, preprocess_image, resolve_device


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def verify_sources(manifest):
    """Revalidate raw bytes and review receipts; JSON identity alone is insufficient."""
    from .dataset import validate_episode
    if manifest.get('provenance') != 'event-aligned-reviewed-v2':
        raise ValueError('Manifest needs reviewed raw-source provenance; export it again')
    episodes = manifest.get('episodes', [])
    if not episodes or len({e['episode_id'] for e in episodes}) != len(episodes):
        raise ValueError('Manifest needs unique raw episodes')
    expected = {'train': [], 'val': []}
    for episode in episodes:
        report = validate_episode(episode['path'], manifest['camera_mode'], manifest['gates'])
        if not report['eligible'] or report['digest'] != episode['digest']:
            raise ValueError(f"Raw source changed or became ineligible: {episode['path']}")
        if (report['meta']['episode_id'] != episode['episode_id'] or report['context']['task'] != episode['task']
                or report['context']['object_id'] != episode['object_id']
                or report['meta']['task_config_hash'] != manifest['task_config_hash']):
            raise ValueError('Raw episode identity/config does not match manifest')
        review = json.loads((Path(episode['path']) / 'review.json').read_text())
        if (review.get('result') != 'success' or review.get('source_digest') != report['digest']
                or not review.get('reviewer') or not review.get('reviewed_at')
                or any(review.get(k) != report['context'][k] for k in ('task', 'destination'))):
            raise ValueError('Raw source success review no longer matches')
        expected[episode['split']].extend(report['samples'])
    for split in expected:
        if sorted(expected[split], key=lambda s: s['id']) != sorted(manifest['splits'][split], key=lambda s: s['id']):
            raise ValueError('Manifest samples differ from validated raw sources')


def read_manifest(path):
    manifest = json.loads(Path(path).read_text())
    if manifest.get('schema_version') != 2 or manifest.get('camera_mode') not in ('csi', 'dual'):
        raise ValueError('Unsupported manifest/camera schema')
    contract = manifest['contract']
    if contract['classes'] != list(CLASSES) or contract['limits'] != [list(x) for x in LIMITS] or contract['manual_step'] != 3 or contract['manual_move_ms'] != 150:
        raise ValueError('Manifest action contract does not match policy')
    if canonical_hash(manifest['task_config']) != manifest['task_config_hash']:
        raise ValueError('Task config fingerprint mismatch')
    if not manifest['task_config'].get('mapping_confirmed'):
        raise ValueError('Physical bin mapping is unconfirmed')
    train, val = manifest['splits']['train'], manifest['splits']['val']
    if not train or (not val and not manifest.get('smoke_only')):
        raise ValueError('Training needs train and independent validation (or explicit smoke_only)')
    if manifest.get('smoke_only') and val:
        raise ValueError('Smoke-only manifests must not claim validation')
    if not manifest.get('smoke_only') and any({s['task'] for s in rows} != set(CLASSES) for rows in (train, val)):
        raise ValueError('Ordinary training requires all three tasks in each split')
    train_episodes = {s['episode_id'] for s in train}
    if train_episodes & {s['episode_id'] for s in val}:
        raise ValueError('Episode leakage across splits')
    episodes = manifest.get('episodes', [])
    for key in ('object_id', 'digest'):
        left = {e[key] for e in episodes if e['split'] == 'train'}
        right = {e[key] for e in episodes if e['split'] == 'val'}
        if left & right:
            raise ValueError(f'{key} leakage across splits')
    for sample in train + val:
        if sample['task'] not in CLASSES or len(sample['actions']) != 6 or any(a not in (-1, 0, 1) for a in sample['actions']):
            raise ValueError('Invalid sample action/task')
        if len(sample['measured']) != 6 or not np.isfinite(sample['measured']).all():
            raise ValueError('Invalid measured state')
    verify_sources(manifest)
    return manifest


class ManifestDataset(Dataset):
    def __init__(self, manifest, split='train', image_size=96, cap_idle=False, seed=42):
        self.manifest, self.image_size = manifest, image_size
        self.samples = list(manifest['splits'][split])
        if cap_idle:
            active = [s for s in self.samples if any(s['actions'])]
            initial_idle = [s for s in self.samples if not any(s['actions']) and s['classification_sample']]
            idle = [s for s in self.samples if not any(s['actions']) and not s['classification_sample']]
            random.Random(seed).shuffle(idle)
            retained_idle = idle[:max(1, len(active)//3)]
            self.samples = active + initial_idle + retained_idle
            self.idle_cap = {'active_rows': len(active), 'preserved_initial_idle_rows': len(initial_idle),
                             'other_idle_rows_before': len(idle), 'other_idle_rows_after': len(retained_idle)}
        self.cameras = ('csi', 'wrist') if manifest['camera_mode'] == 'dual' else ('csi',)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        sample = self.samples[index]
        return (torch.stack([preprocess_image(sample['images'][key], self.image_size) for key in self.cameras]),
                torch.tensor(sample['measured'], dtype=torch.float32),
                torch.tensor(CLASSES.index(sample['task'])),
                torch.tensor(sample['actions'], dtype=torch.long) + 1,
                torch.tensor(bool(sample['classification_sample'])))


def action_metrics(target, prediction):
    """Rows are true -1/hold/+1; columns are predictions in the same order."""
    confusion = np.bincount((target.reshape(-1) * 3 + prediction.reshape(-1)), minlength=9).reshape(3, 3)
    true_count, predicted_count = confusion.sum(1), confusion.sum(0)
    tp = np.diag(confusion)
    f1 = np.divide(2 * tp, true_count + predicted_count, out=np.zeros(3), where=(true_count + predicted_count) != 0)
    recall = np.divide(tp, true_count, out=np.zeros(3), where=true_count != 0)
    return {'labels': [-1, 0, 1], 'confusion': confusion.tolist(), 'macro_f1': float(f1.mean()),
            'balanced_accuracy': float(recall[true_count > 0].mean()) if true_count.any() else None,
            'class_support': true_count.tolist()}


def evaluate_model(model, manifest, split, device, batch_size=32):
    dataset = ManifestDataset(manifest, split, model.image_size)
    if not len(dataset):
        raise ValueError(f'No {split} samples; smoke-only runs have no validation metrics')
    model.eval()
    predictions, targets, class_scores = [], [], []
    with torch.no_grad():
        for images, measured, tasks, labels, mask in DataLoader(dataset, batch_size=batch_size):
            actions, classes = model(images.to(device), measured.to(device), tasks.to(device))
            predictions.extend(actions.argmax(-1).cpu().tolist())
            targets.extend(labels.tolist())
            class_scores.extend(classes.softmax(-1).cpu().tolist())
    pred, target = np.array(predictions), np.array(targets)
    correct, active = pred == target, target != 1
    def ratio(values):
        return float(values.mean()) if values.size else None
    metrics = {'split': split, 'action': action_metrics(target, pred),
               'hold_all_action': action_metrics(target, np.ones_like(pred)), 'samples': len(dataset), 'joint_accuracy': ratio(correct),
               'all_joint_accuracy': ratio(correct.all(axis=1)),
               'active_joint_accuracy': ratio(correct[active]),
               'hold_all_joint_accuracy': ratio(target == 1),
               'hold_all_active_joint_accuracy': ratio((target == 1)[active]),
               'hold_all_all_joint_accuracy': ratio((target == 1).all(axis=1)),
               'per_joint_accuracy': correct.mean(axis=0).tolist(), 'per_task': {}}
    for task in CLASSES:
        rows = np.array([s['task'] == task for s in dataset.samples])
        metrics['per_task'][task] = {'action': action_metrics(target[rows], pred[rows]), 'samples': int(rows.sum()), 'joint_accuracy': ratio(correct[rows]),
                                    'active_joint_accuracy': ratio(correct[rows][active[rows]]),
                                    'hold_all_joint_accuracy': ratio(target[rows] == 1),
                                    'hold_all_active_joint_accuracy': ratio((target[rows] == 1)[active[rows]]),
                                    'all_joint_accuracy': ratio(correct[rows].all(axis=1)),
                                    'per_joint_accuracy': correct[rows].mean(axis=0).tolist() if rows.any() else None}
    # Aggregate only initial-view predictions within each episode, never late grasp/bin views.
    episodes = {}
    for sample, scores in zip(dataset.samples, class_scores):
        if sample['classification_sample']:
            entry = episodes.setdefault(sample['episode_id'], {'task': sample['task'], 'scores': []})
            entry['scores'].append(scores)
    recognition = []
    for entry in episodes.values():
        recognition.append((entry['task'], int(np.mean(entry['scores'], axis=0).argmax()) == CLASSES.index(entry['task'])))
    metrics['recognition'] = {'initial_view_episodes': len(recognition),
        'episode_accuracy': float(np.mean([ok for _, ok in recognition])) if recognition else None,
        'per_task': {task: {'episodes': sum(t == task for t, _ in recognition),
                           'accuracy': float(np.mean([ok for t, ok in recognition if t == task])) if any(t == task for t, _ in recognition) else None} for task in CLASSES}}
    return metrics


def load_checkpoint(checkpoint, manifest_path=None, device='auto'):
    device = resolve_device(device)
    checkpoint = torch.load(checkpoint, map_location='cpu', weights_only=True)
    if checkpoint.get('schema_version') != 2:
        raise ValueError('Unsupported checkpoint')
    manifest = read_manifest(manifest_path) if manifest_path else None
    if manifest and canonical_hash(manifest) != checkpoint['manifest_fingerprint']:
        raise ValueError('Checkpoint requires its exact training manifest; use inference-only preview for new data')
    model = Policy(**checkpoint['policy'])
    model.load_state_dict(checkpoint['state_dict'], strict=True)
    model.to(device).eval()
    return model, checkpoint, manifest, device


def train_manifest(manifest_path, output, device='auto', epochs=10, batch_size=32, seed=42, image_size=96, learning_rate=1e-3, verbose=False):
    if epochs < 1 or batch_size < 1 or learning_rate <= 0:
        raise ValueError('epochs, batch size and learning rate must be positive')
    manifest = read_manifest(manifest_path)
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Run output must be empty')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n')
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    device = resolve_device(device)
    model = Policy(manifest['camera_mode'], image_size).to(device)
    dataset = ManifestDataset(manifest, image_size=image_size, cap_idle=True, seed=seed)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, generator=torch.Generator().manual_seed(seed))
    counts = np.bincount(np.array([s['actions'] for s in dataset.samples]).reshape(-1) + 1, minlength=3)
    weights = np.divide(counts.sum(), np.count_nonzero(counts) * counts, out=np.zeros(3), where=counts > 0)
    class_weights = torch.tensor(weights, dtype=torch.float32, device=device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    losses, history, best = [], [], -1.0
    source_hashes = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                     for name in ('policy.py', 'train.py', 'dataset.py') if Path(__file__).with_name(name).exists()}
    settings = {'seed': seed, 'epochs': epochs, 'batch_size': batch_size, 'learning_rate': learning_rate,
                'idle_cap': dataset.idle_cap, 'classifier_weight': .2,
                'action_class_counts_train_only': counts.tolist(), 'action_class_weights_train_only': weights.tolist(),
                'selection_criterion': 'validation action macro_f1 over fixed [-1,0,1] classes',
                'device': str(device), 'torch_version': str(torch.__version__), 'source_hashes': source_hashes}
    for epoch in range(epochs):
        model.train(); total, count = 0., 0
        for images, measured, tasks, labels, mask in loader:
            images, measured, tasks, labels, mask = [x.to(device) for x in (images, measured, tasks, labels, mask)]
            optimizer.zero_grad()
            actions, classes = model(images, measured, tasks)
            loss = torch.nn.functional.cross_entropy(actions.flatten(0, 1), labels.flatten(), weight=class_weights)
            if mask.any():
                loss = loss + .2 * torch.nn.functional.cross_entropy(classes[mask], tasks[mask])
            if not torch.isfinite(loss):
                raise ValueError('Nonfinite training loss')
            loss.backward(); optimizer.step()
            total += loss.item() * len(images); count += len(images)
        losses.append(total / count)
        verify_sources(manifest)
        metrics = None if manifest['smoke_only'] else evaluate_model(model, manifest, 'val', device, batch_size)
        history.append({'epoch': epoch + 1, 'loss': losses[-1], 'validation': metrics})
        if verbose:
            score = metrics['action']['macro_f1'] if metrics else 'smoke-only'
            print(f'epoch {epoch+1}/{epochs} loss={losses[-1]:.5f} val_macro_f1={score} device={device}',
                  file=sys.stderr,flush=True)
        checkpoint = {'schema_version': 2, 'policy': {'camera_mode': manifest['camera_mode'], 'image_size': image_size},
            'manifest_fingerprint': canonical_hash(manifest), 'task_config_hash': manifest['task_config_hash'],
            'contract': manifest['contract'], 'training': settings, 'epoch': epoch + 1,
            'smoke_only': manifest['smoke_only'], 'metrics': metrics, 'loss': losses[-1],
            'classifier_supervised_rows': sum(bool(s['classification_sample']) for s in dataset.samples),
            'state_dict': {k: v.detach().cpu() for k, v in model.state_dict().items()}}
        torch.save(checkpoint, output / 'last.pt')
        if metrics and metrics['action']['macro_f1'] > best:
            best = metrics['action']['macro_f1']; torch.save(checkpoint, output / 'best.pt')
    report = {'smoke_only': manifest['smoke_only'], 'training_rows_after_idle_cap': len(dataset),
              'manifest_fingerprint': canonical_hash(manifest), 'training': settings, 'history': history,
              'limitation': 'Task-conditioned single-target BC; offline scores do not establish physical sorting or multi-object detection.'}
    (output / 'metrics.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    return report


def evaluate_checkpoint(checkpoint, manifest, split='val', device='auto'):
    model, saved, manifest, device = load_checkpoint(checkpoint, manifest, device)
    if saved['smoke_only'] and split == 'val':
        raise ValueError('Smoke-only checkpoint has no validation evidence')
    return evaluate_model(model, manifest, split, device)


def compare_checkpoints(left_checkpoint, left_manifest, right_checkpoint, right_manifest, device='auto'):
    left, _, a, selected = load_checkpoint(left_checkpoint, left_manifest, device)
    right, _, b, _ = load_checkpoint(right_checkpoint, right_manifest, str(selected))
    if a['smoke_only'] or b['smoke_only']:
        raise ValueError('A smoke-only run cannot supply a camera comparison')
    if a['task_config_hash'] != b['task_config_hash'] or a['contract'] != b['contract']:
        raise ValueError('Camera comparison requires the same task and action contract')
    def validation_identity(manifest):
        episodes = sorted((e['episode_id'], e['digest']) for e in manifest['episodes'] if e['split']=='val')
        samples = sorted((s['id'],tuple(s['measured']),tuple(s['actions'])) for s in manifest['splits']['val'])
        return episodes,samples
    if validation_identity(a) != validation_identity(b):
        raise ValueError('Validation episodes/samples differ; these scores are not a controlled camera comparison')
    return dict(comparable=True, validation_samples=len(a['splits']['val']),
                left=dict(camera_mode=a['camera_mode'],metrics=evaluate_model(left,a,'val',selected)),
                right=dict(camera_mode=b['camera_mode'],metrics=evaluate_model(right,b,'val',selected)),
                limitation='Offline comparison on the same recorded observations; no physical success-rate evidence')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    train = commands.add_parser('train'); train.add_argument('manifest'); train.add_argument('--output', required=True)
    train.add_argument('--device', default='auto', choices=('auto', 'cpu', 'mps', 'cuda'))
    train.add_argument('--epochs', type=int, default=10); train.add_argument('--batch-size', type=int, default=32)
    train.add_argument('--seed', type=int, default=42); train.add_argument('--image-size', type=int, default=96)
    train.add_argument('--learning-rate', type=float, default=1e-3)
    evaluate = commands.add_parser('evaluate'); evaluate.add_argument('checkpoint'); evaluate.add_argument('manifest')
    evaluate.add_argument('--split', choices=('train', 'val'), default='val'); evaluate.add_argument('--device', default='auto')
    compare = commands.add_parser('compare',help='compare models only on identical held-out samples')
    for name in ('left_checkpoint','left_manifest','right_checkpoint','right_manifest'):
        compare.add_argument(name)
    compare.add_argument('--device',default='auto')
    args = vars(parser.parse_args()); command = args.pop('command')
    try:
        if command == 'train':
            result = train_manifest(args.pop('manifest'), verbose=True, **args)
            result = dict(output=args['output'], device=result['training']['device'],
                          epochs=len(result['history']), last_loss=result['history'][-1]['loss'],
                          smoke_only=result['smoke_only'], metrics=str(Path(args['output'])/'metrics.json'),
                          checkpoint=str(Path(args['output'])/('last.pt' if result['smoke_only'] else 'best.pt')))
        elif command == 'evaluate':
            result = evaluate_checkpoint(**args)
        else:
            result = compare_checkpoints(**args)
    except (ValueError,OSError,KeyError) as error:
        parser.exit(2,f'error: {error}\n')
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
