"""Run one frozen Maxwell task and commit only a complete verified task set."""
import copy
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit

from ephys_modular.contracts import (ContractError, MAX_JSON_BYTES,
    canonical_json, decode_json, digest, make_stage, manifest_uri, parse_file, parse_s3, validate_ref)
from ephys_modular.local import LocalStore
from ephys_modular.publication import load_envelope, publish_stage
from ephys_modular.s3 import IntegrityError, S3Error, S3Store

TOOLS = Path('/opt/ephys-modular/maxwell/scripts')
FAILURE_LOG_TAIL_BYTES = 16 * 1024


class ScientificFailure(RuntimeError):
    """A tool failed without a recognized successful conversion."""
    exit_code = 1


class ScratchExceeded(ScientificFailure):
    pass


def _json_equal(left, right):
    """Frozen JSON retains numeric types, signed zero and nullable key presence."""
    return canonical_json(left) == canonical_json(right)


def _claims_match(observed, expected):
    """Check declared frozen claims; additional report observations stay allowed."""
    return (isinstance(observed, dict)
            and all(key in observed and _json_equal(observed[key], value)
                    for key, value in expected.items()))


def validate_plan(store, plan):
    """Rebuild bindings/allocation from frozen graph; diagnostics may vary by read."""
    from task_preparation import prepare_conversion
    if (not isinstance(plan, dict) or plan.get('document_type') != 'maxwell_conversion_plan'
            or type(plan.get('schema_version')) is not int or plan['schema_version'] != 1
            or plan.get('scientific_execution_complete') is not False
            or plan.get('plan_digest') != digest({k: v for k, v in plan.items() if k != 'plan_digest'})):
        raise ContractError('Invalid or changed conversion plan')
    semantic = plan['semantic']
    options = plan.get('planning_options', {})
    context = dict(plan['execution'], output_base_uri=semantic['dataset_uri'],
        workflow_version=semantic['owner']['workflow_version'],
        runtime={'minimum_disk_gib': options.get('minimum_disk_gib', 0)})
    current = prepare_conversion(store, plan['parent'], semantic['settings']['submitted'],
        context, semantic['owner'],
        metadata_read_budget_bytes=options.get('metadata_read_budget_bytes', 64 * 1024 * 1024),
        read_budgets=options.get('read_budgets'))
    def comparable(value):
        result = copy.deepcopy(value)
        result.pop('plan_digest', None)
        # Accept previously prepared version-one plans without explicit options.
        result.pop('planning_options', None)
        for task in result['tasks']:
            task['header'].pop('inspection', None)
        return result
    if not _json_equal(comparable(current), comparable(plan)):
        raise ContractError('Conversion plan differs from current frozen inputs or derived bindings')
    return plan


def reader_url(uri, transport, public_endpoint=None):
    if transport == 'local':
        parse_file(uri)
        return uri
    bucket, key = parse_s3(uri)
    if not isinstance(public_endpoint, str):
        raise ContractError('S3 publication requires an explicit public reader endpoint')
    endpoint = urlsplit(public_endpoint)
    if (endpoint.scheme not in ('https', 'http') or not endpoint.netloc
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise ContractError('Public reader endpoint must be an absolute HTTP(S) origin/prefix')
    return public_endpoint.rstrip('/') + '/' + bucket + '/' + quote(key, safe='/')


class ScratchGuard:
    """Bound task files and watch free space while a scientific child executes."""
    def __init__(self, directory, allocation):
        self.directory, self.allocation = Path(directory), allocation
        self.peak_bytes = self.peak_allocated_bytes = self.checks = 0
        self.low_free = None
        self.snapshots = []
        self.published_local_files = []

    def check(self, phase=None):
        sizes, allocated = {}, 0
        paths = set(self.directory.rglob('*')) | set(self.published_local_files)
        for path in paths:
            try:
                item = path.lstat()
            except FileNotFoundError:  # a scientific tool atomically replaced its temporary file
                continue
            if stat.S_ISDIR(item.st_mode):
                continue
            if not stat.S_ISREG(item.st_mode):
                raise ScratchExceeded('Task scratch contains a non-regular file')
            try:
                name = str(path.relative_to(self.directory))
            except ValueError:
                name = 'published:' + str(path)
            sizes[name] = item.st_size
            allocated += item.st_blocks * 512
        apparent = sum(sizes.values())
        free = min(shutil.disk_usage(path).free for path in
                   [self.directory] + [path.parent for path in self.published_local_files])
        self.peak_bytes = max(self.peak_bytes, apparent)
        self.peak_allocated_bytes = max(self.peak_allocated_bytes, allocated)
        self.low_free = min(self.low_free, free) if self.low_free is not None else free
        self.checks += 1
        if max(apparent, allocated) > self.allocation['requested_bytes'] - self.allocation['minimum_free_bytes']:
            raise ScratchExceeded('Task scratch exceeds the planned allocation')
        if free < self.allocation['minimum_free_bytes']:
            raise ScratchExceeded('Task scratch has less than the required free-space reserve')
        if phase is not None:
            self.snapshots.append({'phase': phase, 'apparent_bytes': apparent,
                                   'allocated_bytes': allocated, 'free_bytes': free})
        return sizes

    def preflight(self):
        self.check('preflight')
        if shutil.disk_usage(self.directory).free < self.allocation['requested_bytes']:
            raise ScratchExceeded('Task filesystem cannot supply the planned scratch allocation')

    def preflight_local_publication(self, destinations):
        # Outputs may be on another filesystem. Check each actual destination
        # volume as well as the modeled task filesystem, without creating paths.
        volumes = {}
        for uri, source in destinations:
            parent = parse_file(uri).parent
            while not parent.exists():
                parent = parent.parent
            device = parent.stat().st_dev
            current = volumes.setdefault(device, {'parent': parent, 'bytes': 0})
            current['bytes'] += source.stat().st_size
        for value in volumes.values():
            if shutil.disk_usage(value['parent']).free < value['bytes'] + self.allocation['minimum_free_bytes']:
                raise ScratchExceeded('Local publication filesystem lacks output bytes plus the free-space reserve')

    def report(self):
        sizes = self.check('complete_before_evidence')
        return {'requested_bytes': self.allocation['requested_bytes'],
            'minimum_free_bytes': self.allocation['minimum_free_bytes'],
            'observed_peak_apparent_bytes': self.peak_bytes,
            'observed_peak_allocated_bytes': self.peak_allocated_bytes,
            'observed_minimum_free_bytes': self.low_free, 'checks': self.checks,
            'file_bytes': sizes, 'snapshots': self.snapshots}


def _report_phase_failure(name, returncode, log):
    """Put a bounded cause in ordinary task stderr; keep the full phase log."""
    try:
        with log.open('rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - FAILURE_LOG_TAIL_BYTES))
            tail = stream.read(FAILURE_LOG_TAIL_BYTES).decode('utf-8', errors='replace')
        # Tool output may contain terminal escapes or partial UTF-8 at the tail
        # boundary. Preserve readable text, never execute terminal control codes.
        tail = ''.join(c if c in '\n\t' or c.isprintable() else '?' for c in tail)
    except OSError:
        tail = '[phase log could not be read]'
    print(f'[scientific_phase] {name} failed (exit {returncode}); '
          f'last {FAILURE_LOG_TAIL_BYTES} bytes follow; full log: {log.name}', file=sys.stderr)
    print(tail.rstrip('\n'), file=sys.stderr)


def _run(guard, name, command, *, environment=None, transfer=False):
    guard.check('before_' + name)
    log = guard.directory / (name + '.log')
    started = time.monotonic()
    with log.open('xb') as stream:
        child = subprocess.Popen([str(item) for item in command], cwd=guard.directory,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True, env=environment)
        try:
            while child.poll() is None:
                guard.check()
                time.sleep(.2)
            guard.check('after_' + name)
            if child.returncode != 0:
                failure = S3Error if transfer and child.returncode == 76 else ScientificFailure
                raise failure('Guarded phase failed: ' + name +
                    ' (exit ' + str(child.returncode) + '); full log: ' + log.name)
        except BaseException:
            # Stop descendants too: a failed guard may not leave a writer consuming scratch.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            # The original child can exit before a descendant that ignores TERM.
            # Its session still belongs to this phase; no writer may survive it.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _report_phase_failure(name, child.returncode, log)
            raise
        else:
            # Successful tools must not leave a background writer in their session.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return {'phase': name, 'exit_code': 0, 'duration_seconds': time.monotonic() - started,
            'log_file': log.name}


def _source_basename(task):
    original = task['unit']['metadata'].get('original_source', task['raw'])
    uri = original['uri']
    name = parse_file(uri).name if uri.startswith('file:') else parse_s3(uri)[1].rsplit('/', 1)[-1]
    if not name.endswith('.raw.h5') or name in ('.', '..') or '/' in name or '\x00' in name:
        raise ContractError('Original Maxwell basename must identify one .raw.h5 file')
    return name


def _run_fresh_science(plan, task, directory, source, projection, output_reader_url, guard, *, run_phase=None):
    """Maintained conversion phases, shared by both strictly admitted entry points."""
    effective = plan['semantic']['settings']['effective']
    phases = []
    run_phase = (lambda name, command: _run(guard, name, command)) if run_phase is None else run_phase
    def call(name, script, *args):
        phases.append(run_phase(name, [sys.executable, TOOLS / script, *args]))
    raw_path_file = directory / 'raw-path.txt'
    call('validate_input', 'validate_maxwell_input.py', '--input-file', source,
        '--maxwell-raw-dataset-path', task['raw_dataset_path'], '--verify-all-raw-chunks',
        '--output-raw-dataset-path-file', raw_path_file)
    if raw_path_file.read_text().strip() != task['raw_dataset_path']:
        raise ContractError('Scientific raw-path resolution differs from the frozen task')
    converted, nwb = directory / 'converted.nwb', directory / 'output.nwb'
    args = ['--input-file', source, '--output-file', converted,
            '--maxwell-raw-dataset-path', task['raw_dataset_path']]
    for key in ('session_description', 'session_identifier_prefix', 'session_start_time',
                'subject_id', 'subject_species', 'subject_description', 'nwb_chunk_duration_ms',
                'nwb_chunk_channels', 'nwb_buffer_chunks'):
        args += ['--' + key.replace('_', '-'), str(effective[key])]
    if projection:
        args += ['--nwb-metadata-file', projection]
    call('convert', 'convert_maxone_to_nwb.py', *args)
    call('repack', 'repack_nwb_channel_major.py', '--input-file', converted, '--output-file', nwb)
    files = {'nwb': nwb, 'lindi': directory / 'output.nwb.lindi.json',
             'validation': directory / 'output.nwb.validation.json'}
    if effective['create_linked_maxwell_companion']:
        files['companion'] = directory / 'output.linked.raw.h5'
        call('companion', 'create_legacy_maxwell_companion.py', '--input-file', source,
            '--nwb-file', nwb, '--output-file', files['companion'],
            '--nwb-sibling-filename', nwb.name, '--maxwell-raw-dataset-path', task['raw_dataset_path'])
    call('lindi', 'generate_lindi_metadata.py', '--nwb-file', nwb,
        '--output-file', files['lindi'], '--source-url', output_reader_url,
        '--num-dataset-chunks-threshold', str(effective['lindi_num_dataset_chunks_threshold']),
        '--contiguous-dataset-max-chunk-size', str(effective['lindi_contiguous_dataset_max_chunk_size']))
    args = ['--nwb-file', nwb, '--output-file', files['validation']]
    if projection:
        args += ['--metadata-projection', projection]
    call('validate_metadata', 'validate_nwb_metadata.py', *args)
    check_request = {'source': str(source), 'nwb': str(nwb), 'lindi': str(files['lindi']),
        'raw_dataset_path': task['raw_dataset_path'], 'reader_url': output_reader_url}
    if projection:
        check_request['projection'] = str(projection)
    if 'companion' in files:
        check_request['companion'] = str(files['companion'])
    request_file, check_file = directory / 'checks-request.json', directory / 'checks.json'
    request_file.write_bytes(canonical_json(check_request))
    phases.append(run_phase('validate_outputs', [sys.executable,
        Path(__file__).with_name('verify_outputs.py'), request_file, check_file]))
    checks = decode_json(check_file.read_bytes())
    if checks.get('complete') is not True:
        raise ScientificFailure('Output checks did not complete')
    return files, checks, phases


def execute_unit(store, plan, unit_id, directory, *, public_endpoint=None):
    """Publish a task outcome only after unchanged science and all object checks."""
    validate_plan(store, plan)
    tasks = [task for task in plan['tasks'] if task['unit']['unit_id'] == unit_id]
    if len(tasks) != 1:
        raise ContractError('Requested unit is not unique in the frozen conversion plan')
    task = tasks[0]
    transport = getattr(store, 'transport', 's3')
    output_reader_url = reader_url(task['outputs']['nwb'], transport, public_endpoint)
    source_name = _source_basename(task)
    from runtime_identity import identity
    scientific_runtime = identity()
    directory = Path(os.path.abspath(directory))
    # Never silently reuse or replace another attempt's scratch/cache.
    directory.mkdir(parents=True, exist_ok=False)
    guard = ScratchGuard(directory, task['allocation'])
    guard.preflight()
    inputs = directory / 'inputs'
    inputs.mkdir()
    source = inputs / source_name
    observed = store.download(task['raw'], source)
    guard.check('source_staged')
    projection = None
    projection_ref = None
    if 'projection' in task['unit']:
        projection = inputs / 'projection.json'
        projection_ref = store.download(task['unit']['projection'], projection)
        decode_json(projection.read_bytes())
        guard.check('projection_staged')
    # Keep a content identity after staging even for S3 refs that only had ETag/size.
    local = LocalStore()
    staged_source = local.stat(source.as_uri())
    if staged_source['sha256'] != observed['sha256'] or staged_source['size'] != task['raw']['size']:
        raise IntegrityError('Staged source differs from the frozen full download')
    files, checks, phases = _run_fresh_science(
        plan, task, directory, source, projection, output_reader_url, guard)
    local.verify(staged_source)
    if projection:
        # Exact staged bytes are also checked after all scientific tools have read them.
        staged_projection = local.stat(projection.as_uri())
        if staged_projection['sha256'] != projection_ref['sha256']:
            raise IntegrityError('Projection changed during scientific execution')
    validate_plan(store, plan)
    guard.check('before_publication')
    kinds = {'nwb': 'nwb', 'lindi': 'nwb_lindi', 'validation': 'nwb_validation',
             'companion': 'maxwell_companion'}
    artifacts = []
    if transport == 'local':
        guard.preflight_local_publication([(task['outputs'][name], path) for name, path in files.items()])
    for name, path in files.items():
        guard.check()
        ref = store.upload_file(task['outputs'][name], path)
        store.verify(ref)
        if transport == 'local':
            guard.published_local_files.append(parse_file(ref['uri']))
            guard.check('published_' + name)
        artifacts.append({'kind': kinds[name], 'object': ref})
    report = {'schema_version': 1, 'document_type': 'maxwell_conversion_evidence',
        'scientific_execution_complete': True, 'plan_digest': plan['plan_digest'],
        'task_digest': digest(task), 'stage_signature': plan['stage_signature'],
        'unit_id': unit_id, 'owner': plan['semantic']['owner'], 'settings': plan['semantic']['settings'],
        'source': task['raw'], 'staged_source': observed,
        'projection': task['unit'].get('projection'), 'staged_projection': projection_ref,
        'reader_url': output_reader_url, 'public_reader_endpoint': public_endpoint,
        'scientific_runtime': scientific_runtime, 'artifacts': artifacts, 'checks': checks,
        'phases': phases, 'scratch': guard.report()}
    guard.check('before_evidence_publication')
    evidence = store.put_json(task['outputs']['verification'], report)
    store.read_json(evidence['uri'], expected=evidence)
    return {'unit_id': unit_id, 'status': 'complete', 'artifacts': artifacts, 'evidence': evidence}


def _validate_fresh_report(plan, task, outcome, scientific_runtime, report, *, transport):
    """Complete maintained fresh report semantics; performs no storage substitution."""
    if (not isinstance(report, dict) or type(report.get('schema_version')) is not int
            or report['schema_version'] != 1
            or report.get('document_type') != 'maxwell_conversion_evidence'):
        raise ContractError('Unsupported fresh conversion evidence format')
    unit_id = task['unit']['unit_id']
    kinds = {'nwb': 'nwb', 'lindi': 'nwb_lindi', 'validation': 'nwb_validation',
             'companion': 'maxwell_companion'}
    expected_bindings = {'document_type': 'maxwell_conversion_evidence',
        'scientific_execution_complete': True, 'plan_digest': plan['plan_digest'],
        'task_digest': digest(task), 'stage_signature': plan['stage_signature'],
        'unit_id': unit_id, 'owner': plan['semantic']['owner'], 'settings': plan['semantic']['settings'],
        'source': task['raw'], 'projection': task['unit'].get('projection'),
        'artifacts': outcome['artifacts']}
    if not _claims_match(report, expected_bindings):
        raise ContractError('Task evidence differs from the frozen conversion invocation')
    expected = {kinds[name]: uri for name, uri in task['outputs'].items() if name in kinds}
    artifacts = outcome['artifacts']
    if (not isinstance(artifacts, list) or len(artifacts) != len(expected)
            or not _json_equal({item['kind']: item['object']['uri'] for item in artifacts}, expected)):
        raise ContractError('Task artifact coverage or destinations differ from the plan')
    checks = report.get('checks', {})
    shape = task['header']['header']['selected'][0]['shape']
    expected_checks = {'complete': True, 'signal_shape': [shape[1], shape[0]],
        'signal_values_checked': shape[0] * shape[1], 'all_signal_values_equal': True,
        'all_lindi_signal_ranges_equal': True,
        'all_linked_values_equal': True if 'companion' in task['outputs'] else None,
        'modeled_projection_verified': 'projection' in task['unit']}
    if (not _claims_match(checks, expected_checks)
            or type(checks.get('lindi_signal_ranges_checked')) is not int
            or checks['lindi_signal_ranges_checked'] <= 0):
        raise ContractError('Task lacks complete scientific output verification')
    expected_phases = ['validate_input', 'convert', 'repack']
    if 'companion' in task['outputs']:
        expected_phases += ['companion']
    expected_phases += ['lindi', 'validate_metadata', 'validate_outputs']
    phases = report.get('phases')
    if (not isinstance(phases, list) or any(not isinstance(item, dict) for item in phases)
            or not _json_equal([item.get('phase') for item in phases], expected_phases)
            or any(type(item.get('exit_code')) is not int or item['exit_code'] != 0
                   for item in phases)
            or not _json_equal(report.get('scientific_runtime'), scientific_runtime)):
        raise ContractError('Task scientific phases/runtime do not match the owning execution')
    observed = report.get('staged_source')
    validate_ref(observed, strong=True, transport=transport)
    if not _claims_match(observed, task['raw']):
        raise ContractError('Task lacks the full frozen source verification')
    if 'projection' in task['unit']:
        observed_projection = report.get('staged_projection')
        validate_ref(observed_projection, strong=True, transport=transport)
        if not _claims_match(observed_projection, task['unit']['projection']):
            raise ContractError('Task lacks the full frozen projection verification')
    if report.get('reader_url') != reader_url(task['outputs']['nwb'], transport,
                                             report.get('public_reader_endpoint')):
        raise ContractError('Task reader URL differs from its durable NWB destination')
    scratch = report.get('scratch', {})
    allocation = task['allocation']
    if (not _claims_match(scratch, {key: allocation[key] for key in ('requested_bytes', 'minimum_free_bytes')})
            or any(type(scratch.get(key)) is not int or scratch[key] < 0
                   for key in ('observed_peak_apparent_bytes', 'observed_peak_allocated_bytes',
                               'observed_minimum_free_bytes', 'checks'))
            or scratch['checks'] == 0
            or max(scratch['observed_peak_apparent_bytes'], scratch['observed_peak_allocated_bytes'])
               > allocation['requested_bytes'] - allocation['minimum_free_bytes']
            or scratch['observed_minimum_free_bytes'] < allocation['minimum_free_bytes']):
        raise ContractError('Task lacks valid bounded scratch usage evidence')
    return report


def _validate_fresh_task(store, plan, task, outcome, scientific_runtime):
    """Apply the complete maintained fresh outcome checks to one planned task.

    The caller validates the actual plan and freezes the installed runtime first.
    This helper neither accepts other proof kinds nor publishes a stage/envelope.
    """
    transport = getattr(store, 'transport', 's3')
    unit_id = task['unit']['unit_id']
    if outcome.get('unit_id') != unit_id:
        raise ContractError('Fresh outcome belongs to another planned unit')
    if outcome.get('status') != 'complete' or set(outcome) != {'unit_id', 'status', 'artifacts', 'evidence'}:
        raise ContractError('Conversion outcomes must be complete owned task outcomes')
    if outcome['evidence']['uri'] != task['outputs']['verification']:
        raise ContractError('Task evidence has a different reviewed destination')
    report, _ = store.read_json(outcome['evidence']['uri'], expected=outcome['evidence'])
    _validate_fresh_report(plan, task, outcome, scientific_runtime, report, transport=transport)
    artifacts = outcome['artifacts']
    for artifact in artifacts:
        store.verify(artifact['object'])
    return outcome


def commit_conversion(store, plan, outcomes):
    """Return the next envelope only after every planned task has verified evidence."""
    validate_plan(store, plan)
    transport = getattr(store, 'transport', 's3')
    tasks = {task['unit']['unit_id']: task for task in plan['tasks']}
    if (not isinstance(outcomes, list) or len(outcomes) != len(tasks)
            or any(not isinstance(outcome, dict) for outcome in outcomes)
            or {outcome.get('unit_id') for outcome in outcomes} != set(tasks)):
        raise ContractError('Conversion commit requires exactly every planned unit once')
    from runtime_identity import identity
    scientific_runtime = identity()
    by_id = {outcome['unit_id']: outcome for outcome in outcomes}
    ordered = []
    for unit_id, task in tasks.items():
        ordered.append(_validate_fresh_task(store, plan, task, by_id[unit_id], scientific_runtime))
    semantic = plan['semantic']
    stage = make_stage(semantic['contract'], semantic['kind'], semantic['variant'],
        semantic['dataset_uri'], semantic['selection'], semantic['owner'],
        semantic['settings']['submitted'], semantic['settings']['effective'], plan['execution'],
        semantic['unit_inputs'], ordered, semantic['inputs'], transport=transport)
    if stage['signature'] != plan['stage_signature']:
        raise ContractError('Committed stage has a different planned scientific identity')
    envelope, parent = load_envelope(store, plan['parent'], semantic['dataset_uri'])
    root, execution = semantic['dataset_uri'], plan['execution']
    base_args = (root, execution['run_id'], execution['step_id'])
    _, result = publish_stage(store, envelope, parent, stage,
        manifest_uri(*base_args, name='result.json', transport=transport),
        manifest_uri(*base_args, transport=transport), set(tasks))
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('unit', 'commit'))
    parser.add_argument('--plan-file', required=True)
    parser.add_argument('--transport', choices=('s3', 'local'), default='s3')
    parser.add_argument('--s3-endpoint')
    parser.add_argument('--aws-profile')
    parser.add_argument('--public-reader-endpoint')
    parser.add_argument('--unit-id')
    parser.add_argument('--work-dir')
    parser.add_argument('--outcome-file', action='append', default=[])
    args = parser.parse_args()
    def read(path):
        with open(path, 'rb') as stream:
            return decode_json(stream.read(MAX_JSON_BYTES + 1))
    plan = read(args.plan_file)
    root = plan['semantic']['dataset_uri']
    store = (LocalStore if args.transport == 'local' else S3Store)(
        output_root=root, endpoint=args.s3_endpoint, profile=args.aws_profile)
    if args.operation == 'unit':
        if not args.unit_id or not args.work_dir or len(args.outcome_file) != 1:
            parser.error('unit requires --unit-id, --work-dir and one --outcome-file')
        outcome = execute_unit(store, plan, args.unit_id, args.work_dir,
                               public_endpoint=args.public_reader_endpoint)
        target = Path(os.path.abspath(args.outcome_file[0]))
        result = LocalStore(target.parent.as_uri() + '/').put_json(target.as_uri(), outcome)
    else:
        if args.unit_id or args.work_dir or not args.outcome_file:
            parser.error('commit requires outcome files and no unit/work directory')
        result = commit_conversion(store, plan, [read(path) for path in args.outcome_file])
    print(result['uri'])


if __name__ == '__main__':
    try:
        main()
    except (S3Error, ScientificFailure, ContractError) as error:
        print('[conversion_worker] ' + str(error), file=sys.stderr)
        raise SystemExit(getattr(error, 'exit_code', 2)) from None
