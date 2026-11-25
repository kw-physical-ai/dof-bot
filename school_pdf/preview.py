"""Write offline policy proposals; never connects to or actuates a robot."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import torch
from .policy import CLASSES, LIMITS
from .train import ManifestDataset, canonical_hash, load_checkpoint, read_manifest


def preview(checkpoint, manifest, output, split='val', device='auto', task=None,
            confidence_threshold=.8, single_target=False, inference_only=False):
    if not 0 <= confidence_threshold <= 1:
        raise ValueError('confidence_threshold must be in [0,1]')
    if task is not None and task not in (*CLASSES, 'auto'):
        raise ValueError('Unknown task')
    model, saved, bound, device = load_checkpoint(checkpoint, None if inference_only else manifest, device)
    data = read_manifest(manifest) if bound is None else bound
    if data['camera_mode'] != saved['policy']['camera_mode'] or data['task_config_hash'] != saved['task_config_hash'] or data['contract'] != saved['contract']:
        raise ValueError('Preview camera/config/action contract differs from checkpoint')
    dataset = ManifestDataset(data, split, model.image_size)
    if not len(dataset):
        raise ValueError(f'No {split} samples')
    rows, episode_states = [], {}
    # Process each episode causally even if a caller supplied interleaved sample rows.
    indices = sorted(range(len(dataset)), key=lambda i: (dataset.samples[i]['episode_id'], dataset.samples[i]['step_id']))
    with torch.no_grad():
        for index in indices:
            sample = dataset.samples[index]
            images, measured, task_id, _, _ = dataset[index]
            images, measured = images[None].to(device), measured[None].to(device)
            reason, status, confidence, predicted_task = None, 'proposal', None, None
            chosen = task or sample['task']
            if task == 'auto':
                state = episode_states.setdefault(sample['episode_id'], {'scores': [], 'decided': False, 'task': None, 'reason': None})
                if sample['classification_sample'] and not state['decided']:
                    _, classes = model(images, measured, torch.zeros(1, dtype=torch.long, device=device))
                    state['scores'].append(classes.softmax(-1)[0])
                    status = 'observe'
                if state['scores']:
                    confidence_tensor, predicted = torch.stack(state['scores']).mean(0).max(0)
                    confidence, predicted_task = float(confidence_tensor), CLASSES[int(predicted)]
                if not state['decided'] and not sample['classification_sample']:
                    state['decided'] = True
                    if not saved.get('classifier_supervised_rows'):
                        state['reason'] = 'Checkpoint has no initial-view classifier supervision'
                    elif not single_target:
                        state['reason'] = 'Single target not asserted; this classifier cannot detect multiple objects'
                    elif confidence is None or confidence < confidence_threshold:
                        state['reason'] = 'Missing or ambiguous initial-view material confidence'
                    else:
                        state['task'] = predicted_task
                chosen = state['task']
                reason = state['reason']
                if not single_target or not saved.get('classifier_supervised_rows'):
                    reason = reason or 'Single target assertion and classifier supervision required'
                if reason:
                    status = 'stop'
                elif not state['decided']:
                    status = 'observe'
            else:
                _, classes = model(images, measured, task_id[None].to(device))
                confidence_tensor, predicted = classes.softmax(-1)[0].max(0)
                confidence, predicted_task = float(confidence_tensor), CLASSES[int(predicted)]
            actions, targets, action_confidence, command_mask = None, None, None, None
            if status == 'proposal':
                logits, _ = model(images, measured, torch.tensor([CLASSES.index(chosen)], device=device))
                actions = (logits.argmax(-1)[0].cpu() - 1).tolist()
                action_confidence = logits.softmax(-1).max(-1).values[0].cpu().tolist()
                command_mask = [bool(action and not (value <= lo and action < 0 or value >= hi and action > 0))
                                for value,action,(lo,hi) in zip(sample['measured'],actions,LIMITS)]
                targets = [max(lo,min(hi,value+action*3)) if send else None
                           for value,action,(lo,hi),send in zip(sample['measured'],actions,LIMITS,command_mask)]
            rows.append({'id': sample['id'], 'episode_id': sample['episode_id'], 'step_id': sample['step_id'],
                         'task': chosen, 'recognized_material': predicted_task, 'recognition_confidence': confidence,
                         'action_confidence': action_confidence,
                         'classification_sample': sample['classification_sample'], 'status': status,
                         'reason': reason, 'actions': actions, 'command_mask':command_mask, 'proposed_targets': targets,
                         'measured': sample['measured'], 'images': sample['images']})
    result = {'offline_only': True, 'inference_only': inference_only, 'manifest_fingerprint': canonical_hash(data),
              'checkpoint_manifest_fingerprint': saved['manifest_fingerprint'], 'split': split,
              'limitation': 'Single-target task-conditioned BC. Confidence cannot certify object count, material, safety, or sorting success.',
              'rows': rows}
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('Preview output already exists')
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkpoint'); parser.add_argument('manifest'); parser.add_argument('--output', required=True)
    parser.add_argument('--split', default='val', choices=('train', 'val')); parser.add_argument('--device', default='auto')
    parser.add_argument('--task', choices=(*CLASSES, 'auto'))
    parser.add_argument('--confidence-threshold', type=float, default=.8)
    parser.add_argument('--single-target', action='store_true', help='Assert one target is present; no detector verifies this')
    parser.add_argument('--inference-only', action='store_true', help='Allow another manifest with identical camera/config contract; provides no held-out evaluation evidence')
    try:
        result = preview(**vars(parser.parse_args()))
    except (ValueError,OSError,KeyError) as error:
        parser.exit(2,f'error: {error}\n')
    print(json.dumps({'output_rows': len(result['rows']), 'stopped_rows': sum(r['status'] == 'stop' for r in result['rows']), 'offline_only': True}))


if __name__ == '__main__':
    main()
