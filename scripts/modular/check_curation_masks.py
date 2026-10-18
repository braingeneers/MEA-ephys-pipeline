#!/usr/bin/env python3
"""Replay pinned/fixed curation on retained real empty and nonempty analyzers.

Use the pinned base image, original task trees read-only and a fresh output root.
This replays curation/serialization, not sorting or postprocessing.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import spikeinterface as si

from check_stage_science import Comparison, compare_products

SOURCE = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root):
    return {path.relative_to(root).as_posix(): sha(path)
            for path in sorted(root.rglob('*')) if path.is_file()}


def only(paths):
    values = list(paths)
    assert len(values) == 1, values
    return values[0]


def replay(root, name, code, task):
    case = root / name
    (case / 'capsule').mkdir(parents=True)
    shutil.copytree(code, case / 'capsule/code')
    (case / 'capsule/data').symlink_to(task / 'capsule/data')
    (case / 'capsule/results').mkdir()
    (case / 'capsule/scratch').mkdir()
    command = ['./run', '--n-jobs', '2']
    (case / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    with (case / 'capsule.log').open('w') as log:
        result = subprocess.run(command, cwd=case / 'capsule/code', stdout=log,
                                stderr=subprocess.STDOUT, timeout=180)
    assert result.returncode == 0, (name, (case / 'capsule.log').read_text()[-4000:])
    path = only((case / 'capsule/results').glob('qc_*.npy'))
    mask = np.load(path, allow_pickle=False)
    receipt = {'exit': result.returncode, 'mask_path': str(path), 'mask_dtype': str(mask.dtype),
               'mask_shape': list(mask.shape), 'passing': int(mask.sum()), 'mask_sha256': sha(path),
               'result_directory': str(case / 'capsule/results')}
    (case / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt, mask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('empty-task', 'nonempty-task', 'original-code', 'original-license', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    fixed = SOURCE / 'capsules/curation/code'
    old = (args.original_code / 'run_capsule.py').read_text()
    before = 'default_qc = np.array([True if unit in curated_unit_ids else False for unit in analyzer.sorting.unit_ids])'
    after = before[:-1] + ', dtype=bool)'
    assert old.count(before) == 1
    expected = '\n'.join(line.rstrip() for line in old.replace(before, after, 1).splitlines()) + '\n'
    assert (fixed / 'run_capsule.py').read_text() == expected, 'Unexpected scientific curation source change'
    for name in ('run', 'params.json'):
        assert (fixed / name).read_bytes() == (args.original_code / name).read_bytes(), name
    assert (SOURCE / 'capsules/curation/LICENSE').read_bytes() == args.original_license.read_bytes()
    query = json.loads((fixed / 'params.json').read_text())['query']
    cases, masks, source_inputs = {}, {}, {}
    for kind, task, units in (('empty', args.empty_task, 0), ('nonempty', args.nonempty_task, 15)):
        analyzer_path = only((task / 'capsule/data').glob('postprocessed_*.zarr')).resolve()
        before_files = inventory(analyzer_path)
        analyzer = si.load_sorting_analyzer(analyzer_path)
        assert analyzer.get_num_units() == units and analyzer.recording is not None
        ids = analyzer.get_extension('quality_metrics').get_data().query(query).index.values
        expected_mask = np.isin(analyzer.unit_ids, ids)
        for version, code in (('old', args.original_code), ('fixed', fixed)):
            name = kind + '-' + version
            cases[name], masks[name] = replay(args.output, name, code, task)
        assert masks[kind + '-fixed'].dtype == np.dtype(bool)
        np.testing.assert_array_equal(masks[kind + '-fixed'], expected_mask)
        assert inventory(analyzer_path) == before_files, 'Curation modified source analyzer/metrics'
        source_inputs[kind] = {'analyzer': str(analyzer_path), 'units': units,
                               'files': before_files, 'query': query, 'unchanged_after_both_replays': True}
        print(kind, 'old/fixed CLI PASS', flush=True)
    assert masks['empty-old'].dtype == np.dtype('float64') and masks['empty-old'].shape == (0,)
    assert masks['empty-fixed'].shape == (0,)
    np.testing.assert_array_equal(masks['nonempty-old'], masks['nonempty-fixed'])
    assert masks['nonempty-old'].dtype == np.dtype(bool)
    check = Comparison()
    compare_products(check, 'nonempty', Path(cases['nonempty-old']['result_directory']),
                     Path(cases['nonempty-fixed']['result_directory']))
    assert not check.differences, check.differences
    empty = Comparison()
    compare_products(empty, 'empty', Path(cases['empty-old']['result_directory']),
                     Path(cases['empty-fixed']['result_directory']))
    assert [item['field'] for item in empty.differences] == [
        'empty/' + Path(cases['empty-old']['mask_path']).name], empty.differences
    result = {'passed': True, 'actual_curation_replays': 4, 'new_sorting_or_postprocessing': False,
              'only_behavior_change_explicit_mask_dtype': True, 'runner_parameters_license_unchanged': True,
              'old_empty_mask_reproduced': True, 'nonempty_masks_classification_metadata_unchanged': True,
              'source_analyzers_metrics_unchanged': True, 'cases': cases, 'source_inputs': source_inputs,
              'nonempty_checked': check.checked, 'nonempty_differences': check.differences,
              'empty_checked': empty.checked, 'empty_intended_difference': empty.differences,
              'execution_fields': sorted(set(check.execution_fields + empty.execution_fields)),
              'original_source_sha256': sha(args.original_code / 'run_capsule.py'),
              'fixed_source_sha256': sha(fixed / 'run_capsule.py')}
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': True, 'replays': 4, 'nonempty_checks': len(check.checked)}, indent=2))


if __name__ == '__main__':
    main()
