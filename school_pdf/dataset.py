"""Inspect, review and export event-aligned demonstrations without robot imports."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
from datetime import datetime, timezone
import uuid

import cv2

CLASSES = ['paper', 'plastic', 'vinyl']
LIMITS = [[0, 180]] * 4 + [[0, 270], [0, 180]]
DEFAULT_GATES = dict(max_camera_age_ms=250, max_camera_skew_ms=200,
                     max_feedback_age_ms=600, max_write_delay_ms=250)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def config_hash(config):
    return hashlib.sha256(_canonical(config)).hexdigest()


def _check_config(config):
    if not isinstance(config, dict) or config.get('version') != 1:
        raise ValueError('task config version must be 1')
    if not isinstance(config.get('mapping_confirmed'), bool):
        raise ValueError('task config needs boolean mapping_confirmed')
    if not isinstance(config.get('layout_id'), str) or not config['layout_id'].strip():
        raise ValueError('task config needs layout_id')
    tasks = config.get('tasks', {})
    if set(tasks) != set(CLASSES):
        raise ValueError('task config must define paper, plastic and vinyl')
    destinations = []
    for task in CLASSES:
        destination = tasks[task].get('destination') if isinstance(tasks[task], dict) else None
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError(f'{task} needs a nonempty destination name')
        destinations.append(destination.strip())
    if len(set(destinations)) != len(CLASSES):
        raise ValueError('material destinations must be distinct')
    return config


def load_config(path=None):
    path = Path(path) if path else Path(__file__).with_name('task_config.json')
    return _check_config(json.loads(path.read_text()))


def _atomic_json(path, value):
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path):
    """Bind review to the complete raw artifact, including unreferenced extra files."""
    digest = hashlib.sha256()
    for file in sorted(path.rglob('*')):
        if file.is_file() and file.name != 'review.json' and not file.name.startswith('.review.json.'):
            relative = file.relative_to(path).as_posix().encode()
            digest.update(len(relative).to_bytes(8, 'big'))
            digest.update(relative)
            data = file.read_bytes()
            digest.update(len(data).to_bytes(8, 'big'))
            digest.update(data)
    return digest.hexdigest()


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _image(path, relative):
    if not isinstance(relative, str):
        raise ValueError('image path must be a string')
    file = (path / relative).resolve()
    if not file.is_relative_to(path.resolve()):
        raise ValueError('image path escapes episode')
    if not file.is_file() or cv2.imread(str(file)) is None:
        raise ValueError(f'missing or undecodable image: {relative}')
    return str(file)



def _inspect_legacy(path, report):
    """Audit archived compatibility data without inventing event/state provenance."""
    errors, warnings = report['errors'], report['warnings']
    counts = report['counts'] = dict(frames=0, timing_rows=0, steps=0, samples=0, rejected_samples=0, images={})
    observations = report['source_observations'] = dict(
        action_labels='command targets only; measured feedback and command causality unavailable',
        physical_outcome='not established by stored frames or target labels',
        recorded_result=report['meta'].get('result', 'unreviewed'),
        termination_reason=report['meta'].get('reason'), cameras={})
    warnings.append('Legacy recordings remain excluded from training even when compatibility integrity passes')
    rows, frames = [], []
    for name, destination in (('labels.csv', rows), ('frames.csv', frames)):
        try:
            with (path/name).open(newline='') as source:
                destination.extend(csv.DictReader(source))
        except (OSError, csv.Error) as error:
            errors.append(f'{name}: {error}')
    counts.update(frames=len(rows), timing_rows=len(frames))
    declared = report['meta'].get('frames')
    if not _integer(declared) or declared < 1 or declared != len(rows):
        errors.append('legacy compatibility label count disagrees with metadata')
    if len(rows) != len(frames):
        errors.append('legacy compatibility label/timing row counts disagree')
    target_ranges = [[None,None] for _ in range(6)]
    label_seconds = []
    for index, row in enumerate(rows, 1):
        try:
            if int(row['frame_id']) != index:
                errors.append(f'legacy labels frame {index}: noncontiguous frame_id')
            for joint in range(6):
                value = float(row[f'm{joint+1}'])
                if not _number(value):
                    raise ValueError('nonfinite command target')
                low, high = target_ranges[joint]
                target_ranges[joint] = [value if low is None else min(low,value), value if high is None else max(high,value)]
            stamp = row.get('timestamp', '')
            if len(stamp) >= 6:
                hours, minutes, seconds = int(stamp[:2]),int(stamp[2:4]),float(stamp[4:])
                if not (0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 60):
                    raise ValueError('invalid label clock timestamp')
                label_seconds.append(hours*3600+minutes*60+seconds)
        except (ValueError, TypeError, KeyError) as error:
            errors.append(f'legacy labels frame {index}: {error}')
    observations['command_target_ranges'] = target_ranges
    camera_dirs = sorted(p for p in path.glob('cam[12]') if p.is_dir())
    if not camera_dirs:
        errors.append('legacy recording has no camera directories')
    spans = []
    for directory in camera_dirs:
        camera = directory.name
        files = sorted(directory.glob('*.jpg'))
        counts['images'][camera] = len(files)
        decoded, shapes = 0, set()
        for image in files:
            frame = cv2.imread(str(image))
            if frame is None:
                errors.append(f'legacy missing or undecodable image: {camera}/{image.name}')
            else:
                decoded += 1
                shapes.add(tuple(frame.shape))
        expected = {f'{i:05d}.jpg' for i in range(1,len(rows)+1)}
        if {image.name for image in files} != expected:
            errors.append(f'legacy {camera} image names/count do not match label frame ids')
        receives = []
        for index, row in enumerate(frames,1):
            try:
                value = float(row[f'{camera}_receive_monotonic'])
                if int(row['frame_id']) != index or not _number(value) or value <= 0 or (receives and value <= receives[-1]):
                    raise ValueError('noncontiguous frame id or nonmonotonic receive time')
                receives.append(value)
            except (ValueError,TypeError,KeyError) as error:
                errors.append(f'legacy {camera} timing frame {index}: {error}')
        if len(receives) > 1:
            spans.append(receives[-1]-receives[0])
        observations['cameras'][camera] = dict(decoded_images=decoded,image_shapes=[list(x) for x in sorted(shapes)],
            timing_rows=len(receives),receive_span_seconds=receives[-1]-receives[0] if receives else None)
    if spans:
        report['duration_seconds'] = max(spans)
        observations['duration_source'] = 'host monotonic receive timestamp span; excludes time before/after saved frames'
    elif label_seconds:
        elapsed = 0.0
        for earlier,later in zip(label_seconds,label_seconds[1:]):
            elapsed += (later-earlier) % 86400
        report['duration_seconds'] = elapsed
        observations['duration_source'] = 'label wall clock span; midnight wrap assumed, no monotonic timing available'
    else:
        report['duration_seconds'] = None
        observations['duration_source'] = 'unavailable'
    review_path = path/'review.json'
    if review_path.exists():
        try:
            review = json.loads(review_path.read_text())
            observations['operator_annotation'] = dict(result=review.get('result'),reviewer=review.get('reviewer'),
                digest_matches=review.get('source_digest') == report['digest'])
        except (OSError, ValueError, AttributeError) as error:
            warnings.append(f'Unreadable legacy operator review: {error}')
    report['compatibility_integrity'] = len(errors) == 1


def validate_episode(path, camera_mode='csi', gates=None):
    path = Path(path).resolve()
    if camera_mode not in ('csi', 'dual'):
        raise ValueError('camera_mode must be csi or dual')
    gates = {**DEFAULT_GATES, **(gates or {})}
    if set(gates) != set(DEFAULT_GATES) or any(not _number(x) or x < 0 for x in gates.values()):
        raise ValueError('gates must contain finite nonnegative known millisecond thresholds')
    result = dict(eligible=False, legacy=False, errors=[], warnings=[], samples=[],
                  digest='', context={}, meta={}, counts={})
    errors = result['errors']
    def fail(message):
        errors.append(str(message))
    if not path.is_dir():
        fail(f'episode directory missing: {path}')
        return result
    try:
        result['digest'] = _digest(path)
        meta = json.loads((path / 'meta.json').read_text())
        if not isinstance(meta, dict):
            raise ValueError('metadata must be an object')
        result['meta'] = meta
        result['context'] = context = meta.get('recording_context', {})
        if meta.get('schema_version') != 2:
            result['legacy'] = True
            fail('legacy target-only episode cannot supply causal training labels')
            _inspect_legacy(path, result)
            return result
        uuid.UUID(meta['episode_id'])
        if meta.get('status') != 'complete':
            fail('episode must be finalized with status complete')
        if not isinstance(meta.get('safety_events'), list) or meta['safety_events']:
            fail('episode has safety events or missing safety event record')
        result['warnings'].extend(str(note) for note in meta.get('quality_notes', []))
        config = _check_config(meta.get('task_config'))
        if config_hash(config) != meta.get('task_config_hash'):
            fail('task config hash mismatch')
        if not config['mapping_confirmed']:
            fail('physical task mapping is unconfirmed')
        if not isinstance(context, dict):
            raise ValueError('recording_context must be an object')
        if context.get('bin_layout') != config['layout_id']:
            fail('recording bin_layout does not match embedded layout_id')
        task = context.get('task')
        if task not in CLASSES or context.get('destination') != config['tasks'].get(task, {}).get('destination'):
            fail('task/destination does not match embedded config')
        for key in ('object_id', 'position_id', 'scene_id', 'operator', 'camera_setup', 'bin_layout'):
            if not isinstance(context.get(key), str) or not context[key].strip():
                fail(f'missing recording context: {key}')
        if meta.get('limits') != LIMITS or meta.get('manual_step') != 3 or meta.get('manual_move_ms') != 150:
            fail('unsupported action/servo contract')
        with (path / 'labels.csv').open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        with (path / 'frames.csv').open(newline='') as stream:
            frames = list(csv.DictReader(stream))
        count = meta.get('frames')
        if not _integer(count) or count <= 0 or len(rows) != count or len(frames) != count:
            fail('compatibility frame counts disagree')
        camera_folders = ['cam2'] if camera_mode == 'csi' else ['cam1', 'cam2']
        # Validate every available compatibility stream, even for CSI-only training.
        camera_folders = sorted(set(camera_folders + [x.name for x in path.glob('cam[12]') if x.is_dir()]))
        compatibility_times = {}
        for index, (row, frame) in enumerate(zip(rows, frames), 1):
            if int(row['frame_id']) != index or int(frame['frame_id']) != index:
                fail('compatibility frame ids must be contiguous and aligned')
            if not all(_number(float(row[f'm{i}'])) for i in range(1, 7)):
                fail(f'invalid compatibility targets at frame {index}')
            for camera in camera_folders:
                _image(path, f'{camera}/{index:05d}.jpg')
                value = float(frame[f'{camera}_receive_monotonic'])
                if not _number(value) or value <= compatibility_times.get(camera, 0):
                    fail(f'invalid/nonmonotonic compatibility receive time at frame {index}')
                compatibility_times[camera] = value
        for camera in camera_folders:
            if len(list((path / camera).glob('*.jpg'))) != count:
                fail(f'{camera} image count disagrees with metadata')
        steps = [json.loads(line) for line in (path / 'steps.jsonl').read_text().splitlines() if line.strip()]
        result['counts'] = dict(frames=len(rows), steps=len(steps), samples=0, rejected_samples=0)
        if meta.get('steps') != len(steps):
            fail('step count disagrees with metadata')
        previous_id = 0
        previous_end = -1
        first_observation = None
        movement_started = False
        previous_sources = {}
        last_sample_sources = None
        for step in steps:
            before = len(errors)
            sid = step.get('id')
            if not _integer(sid) or sid <= previous_id:
                fail('step ids must be strictly increasing positive integers')
            else:
                previous_id = sid
            kind = step.get('kind')
            if kind not in ('manual', 'idle', 'home'):
                fail(f'step {sid}: unknown kind')
            observation, start, end = (step.get(k) for k in ('observation_ns', 'feedback_start_ns', 'feedback_end_ns'))
            if not all(_integer(v) and v > 0 for v in (observation, start, end)):
                fail(f'step {sid}: invalid timing')
                continue
            if not start <= end <= observation or start < previous_end:
                fail(f'step {sid}: feedback/observation chronology invalid')
            first_observation = observation if first_observation is None else first_observation
            initial_view = observation-first_observation <= 2_000_000_000 and not movement_started
            if observation - start > gates['max_feedback_age_ms'] * 1e6:
                fail(f'step {sid}: stale feedback')
            measured = step.get('measured')
            if not isinstance(measured, list) or len(measured) != 6 or not all(_number(v) and -3 <= v <= LIMITS[i][1]+3 for i,v in enumerate(measured)):
                fail(f'step {sid}: all six finite measured states required')
                measured = None
            sources = step.get('sources')
            if not isinstance(sources, list) or len(sources) != 6 or not all(isinstance(v,str) and v.strip() for v in sources):
                fail(f'step {sid}: all six feedback sources required')
            elif any(any(word in source.lower() for word in ('target', 'cached', 'command')) for source in sources):
                fail(f'step {sid}: command targets/cached state cannot substitute for measured feedback')
            elif measured is not None and any((value < 0 or value > LIMITS[i][1]) and not any(word in sources[i].lower() for word in ('boundary', 'estimate')) for i,value in enumerate(measured)):
                fail(f'step {sid}: out-of-limit feedback requires disclosed boundary estimate source')
            if step.get('complete') is not True or step.get('blocked'):
                fail(f'step {sid}: incomplete or blocked command batch')
            images = {}
            receives = []
            selected_sources = []
            for camera in (('csi', 'wrist') if camera_mode == 'dual' else ('csi',)):
                info = step.get('images', {}).get(camera, {})
                try:
                    images[camera] = _image(path, info.get('path'))
                    receive = info.get('receive_ns')
                    source = info.get('source_id')
                    if not _integer(receive) or not 0 <= observation-receive <= gates['max_camera_age_ms']*1e6:
                        raise ValueError(f'{camera} stale/future receive time')
                    if not _integer(source) or source < 0 or source < previous_sources.get(camera, -1):
                        raise ValueError(f'{camera} nonmonotonic source frame')
                    previous_sources[camera] = source
                    receives.append(receive)
                    selected_sources.append((camera, source))
                except (ValueError, TypeError) as error:
                    fail(f'step {sid}: {error}')
            if receives and max(receives)-min(receives) > gates['max_camera_skew_ms']*1e6:
                fail(f'step {sid}: cameras exceed skew gate')
            actions = [0]*6
            writes = step.get('writes')
            last_write = observation
            if not isinstance(writes, list):
                fail(f'step {sid}: writes must be a list')
                writes = []
            if kind == 'idle' and writes:
                fail(f'step {sid}: idle must have no writes')
            if kind == 'manual' and not writes:
                fail(f'step {sid}: manual must contain actual writes')
            seen = set()
            for write in writes:
                servo = write.get('servo')
                direction = write.get('direction')
                ws, we = write.get('start_ns'), write.get('end_ns')
                if not _integer(servo) or servo not in range(1,7) or servo in seen:
                    fail(f'step {sid}: invalid/duplicate servo')
                    continue
                seen.add(servo)
                if direction not in (-1,1) or isinstance(direction,bool) or write.get('status') != 'sent':
                    fail(f'step {sid}: write was not a valid sent direction')
                else:
                    actions[servo-1] = direction
                if not all(_integer(v) for v in (ws,we)) or not last_write <= ws <= we:
                    fail(f'step {sid}: write occurs before observation or out of order')
                else:
                    if ws-observation > gates['max_write_delay_ms']*1e6:
                        fail(f'step {sid}: write delay exceeds gate')
                    last_write = we
                target = write.get('target')
                if kind == 'manual' and measured is not None:
                    expected = min(LIMITS[servo-1][1], max(0, measured[servo-1]+direction*3)) if direction in (-1,1) else None
                    if not _number(target) or target != expected or target == measured[servo-1] or write.get('duration_ms') != 150:
                        fail(f'step {sid}: target/duration inconsistent with measured +/-3 action')
            previous_end = max(observation, last_write)
            if kind in ('manual','home') and any(actions):
                movement_started = True
            if kind != 'home' and len(errors) == before:
                pair = tuple(selected_sources)
                if pair == last_sample_sources:
                    result['counts']['rejected_samples'] += 1
                    result['warnings'].append(f'step {sid}: repeated selected camera frame pair; dropped sample, actual writes retained in raw record')
                    continue
                last_sample_sources = pair
                result['samples'].append(dict(id=f"{meta['episode_id']}:{sid}", episode_id=meta['episode_id'],
                    episode=str(path), step_id=sid, task=task, images=images, measured=measured,
                    actions=actions, kind=kind, classification_sample=initial_view))
        if not result['samples']:
            fail('no usable manual or idle samples')
        elif not any(any(sample['actions']) for sample in result['samples']):
            fail('no actual moving command samples; an idle-only recording is not a sorting demonstration')
        result['counts']['samples'] = len(result['samples'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        fail(f'malformed episode: {error}')
    result['eligible'] = not errors
    return result


def review_episode(path, result, reviewer, task=None, destination=None, notes=''):
    if result not in ('success', 'failure', 'abort', 'unreviewed'):
        raise ValueError('review result must be success, failure, abort or unreviewed')
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError('reviewer is required')
    path = Path(path).resolve()
    report = validate_episode(path)
    if not report['meta']:
        raise ValueError('cannot review episode without readable metadata')
    context = report['context']
    if not isinstance(context, dict):
        raise ValueError('cannot review malformed recording context')
    task = task or context.get('task')
    destination = destination or context.get('destination')
    if not report['legacy'] and (task != context.get('task') or destination != context.get('destination')):
        raise ValueError('review task/destination must match recording context')
    review = dict(result=result, reviewer=reviewer.strip(), reviewed_at=datetime.now(timezone.utc).isoformat(),
                  task=task, destination=destination, notes=notes, source_digest=report['digest'])
    if _digest(path) != report['digest']:
        raise ValueError('episode changed during review; finish recording before reviewing')
    _atomic_json(path / 'review.json', review)
    return review


def _content_digest(report):
    digest = hashlib.sha256()
    for sample in report['samples']:
        digest.update(_canonical({k:sample[k] for k in ('measured','actions','kind')}))
        for camera, image in sorted(sample['images'].items()):
            digest.update(camera.encode())
            digest.update(Path(image).read_bytes())
    return digest.hexdigest()


def export_dataset(root, output, camera_mode='csi', seed=42, val_fraction=.2, smoke_only=False, gates=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not 0 < val_fraction < 1:
        raise ValueError('val_fraction must be between zero and one')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f'output must be absent or empty: {output}')
    if output == root or root.is_relative_to(output):
        raise ValueError('output must not contain the raw episode root')
    candidates = [root] if (root/'meta.json').is_file() else sorted(p.parent for p in root.rglob('meta.json') if not p.is_relative_to(output))
    if any(output.is_relative_to(path) for path in candidates):
        raise ValueError('output must be outside each raw episode directory')
    reports, exclusions = [], []
    for path in candidates:
        report = validate_episode(path, camera_mode, gates)
        reasons = report['errors'][:]
        try:
            review = json.loads((path/'review.json').read_text())
            if review.get('result') != 'success':
                reasons.append('operator review is not success')
            if review.get('source_digest') != report['digest']:
                reasons.append('review digest does not match raw episode; review again')
            if not review.get('reviewer') or not review.get('reviewed_at'):
                reasons.append('review needs reviewer and reviewed_at')
            if any(review.get(k) != report['context'].get(k) for k in ('task','destination')):
                reasons.append('review context mismatch')
        except (OSError, ValueError, AttributeError):
            reasons.append('missing or malformed operator review')
        if reasons:
            exclusions.append(dict(path=str(path), reasons=reasons))
        else:
            reports.append(report)
    if not reports:
        raise ValueError('no eligible success-reviewed episodes; inspect recordings and review their current digests')
    hashes = {r['meta']['task_config_hash'] for r in reports}
    if len(hashes) != 1:
        raise ValueError('eligible episodes have different task config hashes; export each physical layout separately')
    ids = [r['meta']['episode_id'] for r in reports]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate episode UUIDs; remove duplicate recording copies from export root')
    # Connected components bind shared objects AND byte-identical content across tasks.
    parents = list(range(len(reports)))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    keys = {}
    for i, report in enumerate(reports):
        for key in (('object', report['context']['object_id']), ('content', _content_digest(report))):
            if key in keys:
                parents[find(i)] = find(keys[key])
            else:
                keys[key] = i
    groups = {}
    for i, report in enumerate(reports):
        groups.setdefault(find(i), []).append(report)
    assignment = {}
    rng = random.Random(seed)
    if smoke_only:
        assignment = {key:'train' for key in groups}
    else:
        # Components that span tasks are indivisible; choose a seeded subset whose
        # complement still represents every task. Brute force is unnecessary: seeded
        # attempts cover normal small datasets and a deterministic repair handles singles.
        keys = sorted(groups)
        task_sets = {key:{r['context']['task'] for r in groups[key]} for key in keys}
        for task in CLASSES:
            if sum(task in tasks for tasks in task_sets.values()) < 2:
                raise ValueError(f'{task} needs at least two independent object/content groups for train and validation; use --smoke-only for a training plumbing check')
        best = None
        for _ in range(2000):
            selected = {key for key in keys if rng.random() < val_fraction}
            if all(any(task in task_sets[k] for k in selected) and any(task in task_sets[k] for k in keys if k not in selected) for task in CLASSES):
                score = sum(abs(sum(task in task_sets[k] for k in selected)/sum(task in s for s in task_sets.values())-val_fraction) for task in CLASSES)
                if best is None or score < best[0]:
                    best = (score, selected)
        if best is None:
            def choose(selected):
                if any(all(k in selected for k in keys if task in task_sets[k]) for task in CLASSES):
                    return None
                missing = [task for task in CLASSES if not any(task in task_sets[k] for k in selected)]
                if not missing:
                    return selected
                for key in keys:
                    if missing[0] in task_sets[key] and key not in selected:
                        found = choose(selected | {key})
                        if found is not None:
                            return found
                return None
            selected = choose(set())
            if selected is None:
                raise ValueError('object/content groups cannot form independent three-task train/validation splits')
            best = (0, selected)
        assignment = {key:('val' if key in best[1] else 'train') for key in keys}
    episodes, splits = [], dict(train=[], val=[])
    for key, members in groups.items():
        split = assignment[key]
        for report in members:
            meta, context = report['meta'], report['context']
            episodes.append(dict(episode_id=meta['episode_id'],path=report['samples'][0]['episode'],digest=report['digest'],task=context['task'],object_id=context['object_id'],split=split))
            splits[split].extend(report['samples'])
    for samples in splits.values():
        samples.sort(key=lambda s:(s['episode'],s['step_id']))
    config = reports[0]['meta']['task_config']
    manifest = dict(schema_version=2,provenance='event-aligned-reviewed-v2',camera_mode=camera_mode,smoke_only=smoke_only,seed=seed,
        task_config=config,task_config_hash=config_hash(config),
        contract=dict(classes=CLASSES,limits=LIMITS,manual_step=3,manual_move_ms=150),
        gates={**DEFAULT_GATES,**(gates or {})},episodes=episodes,splits=splits,exclusions=exclusions)
    if any(_digest(Path(r['samples'][0]['episode'])) != r['digest'] for r in reports):
        raise ValueError('raw episode changed during export; finalize recordings and review again')
    output.mkdir(parents=True,exist_ok=True)
    _atomic_json(output/'manifest.json',manifest)
    return output/'manifest.json'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    inspect = sub.add_parser('inspect',help='report integrity; does not assess physical success')
    inspect.add_argument('episode')
    inspect.add_argument('--camera-mode',choices=['csi','dual'],default='csi')
    inspect.add_argument('--output',help='optional detailed JSON report outside the raw episode')
    review = sub.add_parser('review',help='record an operator outcome bound to raw source bytes')
    review.add_argument('episode')
    review.add_argument('--result',required=True,choices=['success','failure','abort','unreviewed'])
    review.add_argument('--reviewer',required=True)
    review.add_argument('--task',choices=CLASSES)
    review.add_argument('--destination')
    review.add_argument('--notes',default='')
    export = sub.add_parser('export')
    export.add_argument('root')
    export.add_argument('--output',required=True)
    export.add_argument('--camera-mode',choices=['csi','dual'],default='csi')
    export.add_argument('--seed',type=int,default=42)
    export.add_argument('--val-fraction',type=float,default=.2)
    export.add_argument('--smoke-only',action='store_true',help='train only; no validation metrics or generalization claim')
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    try:
        result = export_dataset(**args) if command == 'export' else None
        if command == 'inspect':
            episode = Path(args.pop('episode')).resolve()
            output = args.pop('output')
            result = validate_episode(episode,**args)
            if output:
                destination = Path(output).resolve()
                if destination.is_relative_to(episode):
                    raise ValueError('report output must be outside the raw episode')
                destination.parent.mkdir(parents=True,exist_ok=True)
                _atomic_json(destination,result)
            # Avoid thousands of absolute sample paths hiding the useful result.
            result = {key:value for key,value in result.items() if key not in ('samples','meta')}
        elif command == 'review':
            result = review_episode(args.pop('episode'),**args)
        print(json.dumps(result,indent=2) if isinstance(result,dict) else str(result))
    except (ValueError,OSError) as error:
        parser.exit(2,f'error: {error}\n')


if __name__ == '__main__':
    main()
