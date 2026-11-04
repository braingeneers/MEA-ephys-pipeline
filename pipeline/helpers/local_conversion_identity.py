"""Read-only native conversion identity checks shared by MEA stage boundaries.

Only the exported contract library is required. Scientific model/schema/companion
validation stays in maxwell_bridge.verify_local in the converter runtime.
"""
import hashlib
import importlib
import json
from pathlib import Path
import sys


def load_contracts(common_code=None):
    """Load exactly the declared exported contract/schema tree, without fallback."""
    root = (Path(common_code) if common_code is not None else
            Path(__file__).resolve().parents[1] / 'components/maxwell/components/common/python')
    try:
        root = root.resolve(strict=True)
        package = root / 'ephys_modular'
        expected = {name: (package / name).read_bytes() for name in
                    ('__init__.py', 'contracts.py', 'schemas/artifacts-v1.schema.json')}
    except (OSError, TypeError) as error:
        raise ValueError('Missing declared native contract/schema tree') from error
    # An earlier import must not silently select an installed or different tree.
    for name, relative in (('ephys_modular', '__init__.py'),
                           ('ephys_modular.contracts', 'contracts.py')):
        cached = sys.modules.get(name)
        if cached is not None:
            origin = Path(cached.__file__)
            if origin.read_bytes() != expected[relative]:
                raise ValueError('Loaded native contract code differs from the declared tree')
    cached = sys.modules.get('ephys_modular.contracts')
    if cached is not None:
        origin_schema = Path(cached.__file__).parent / 'schemas/artifacts-v1.schema.json'
        if (origin_schema.read_bytes() != expected['schemas/artifacts-v1.schema.json']
                or cached.canonical_json(cached._SCHEMA) !=
                cached.canonical_json(json.loads(expected['schemas/artifacts-v1.schema.json']))):
            raise ValueError('Loaded native contract schema differs from the declared tree')
    sys.path.insert(0, str(root))
    try:
        module = importlib.import_module('ephys_modular.contracts')
    finally:
        sys.path.pop(0)
    # A package imported before its contracts module also pins a search path.
    if Path(module.__file__).read_bytes() != expected['contracts.py']:
        raise ValueError('Loaded native contract code differs from the declared tree')
    if ((Path(module.__file__).parent / 'schemas/artifacts-v1.schema.json').read_bytes()
            != expected['schemas/artifacts-v1.schema.json']):
        raise ValueError('Loaded native contract schema differs from the declared tree')
    return module


def _api():
    return sys.modules.get('ephys_modular.contracts') or load_contracts()


def canonical_json(value):
    return _api().canonical_json(value)


def digest(value):
    return _api().digest(value)


def validate_envelope(*args, **kwargs):
    return _api().validate_envelope(*args, **kwargs)


def validate_stage(*args, **kwargs):
    return _api().validate_stage(*args, **kwargs)


def selected_stage(*args, **kwargs):
    return _api().selected_stage(*args, **kwargs)


PHASES = ['validate_input', 'convert', 'repack', 'companion', 'lindi',
          'validate_metadata', 'validate_outputs']
NAMES = {'nwb': 'output.nwb', 'nwb_lindi': 'output.nwb.lindi.json',
         'nwb_validation': 'output.nwb.validation.json',
         'maxwell_companion': 'output.linked.raw.h5'}


def require(condition, message):
    if not condition:
        raise _api().ContractError(message)


def json_equal(left, right):
    """Compare frozen JSON claims without Python's numeric coercion."""
    return canonical_json(left) == canonical_json(right)


def schema_one(value, description, document_type=None):
    require(isinstance(value, dict) and type(value.get('schema_version')) is int
            and value['schema_version'] == 1
            and (document_type is None or value.get('document_type') == document_type),
            'Unsupported ' + description)


def expected_phases(settings):
    return [phase for phase in PHASES
            if phase != 'companion' or settings['effective']['create_linked_maxwell_companion']]


def completed_phases(report, settings):
    """Native phase status is an integer exit code, never a JSON boolean/float."""
    phases = report.get('phases') if isinstance(report, dict) else None
    return (isinstance(phases, list) and all(isinstance(p, dict) for p in phases)
            and report.get('scientific_execution_complete') is True
            and json_equal([p.get('phase') for p in phases], expected_phases(settings))
            and all(type(p.get('exit_code')) is int and p['exit_code'] == 0 for p in phases))


def verify_native_admission(report, details, stage, native_ref):
    """Bind the owner's verified completion to this relocated local unit.

    Full scientific/source proof validation happens in maxwell_bridge.prepare
    using the owning runtime. This consumer checks the exact saved admission,
    publication and evidence identities without scientific imports or network I/O.
    Historical fresh-only local units retain their original phase checks.
    """
    admission = details.get('native_validation')
    if admission is None:
        require(completed_phases(report, details['settings']), 'Local native science is incomplete')
        return
    fields = {'schema_version', 'document_type', 'basis', 'native_artifacts', 'native_stage',
              'evidence', 'unit_id', 'owner', 'settings', 'artifacts', 'source_current',
              'direct_origin', 'digest'}
    schema_one(admission, 'native admission', 'mea_verified_native_completion')
    require(set(admission) == fields and admission['digest'] == digest(
        {k: v for k, v in admission.items() if k != 'digest'}), 'Changed native admission')
    for key, expected in dict(native_artifacts=details['native_artifacts'],
            evidence=details['native_evidence'], unit_id=details['native_unit']['unit_id'],
            owner=stage['owner'], settings=stage['settings'], native_stage=native_ref).items():
        require(json_equal(admission[key], expected), 'Native admission lost its ' + key + ' binding')
    require(json_equal({a['kind']: a['object'] for a in admission['artifacts']}, details['artifacts']),
            'Native admission artifact coverage changed')
    _api().validate_ref(admission['source_current'], strong=True, transport='local')
    require(any(all(ref.get(k) == admission['source_current'].get(k)
                    for k in ('uri', 'size', 'etag', 'version_id'))
                and ('sha256' not in ref or ref['sha256'] == admission['source_current']['sha256'])
                for ref in details['native_unit']['sources']), 'Native admission source changed')
    kind = report.get('document_type')
    basis = {'maxwell_conversion_evidence': 'owned_conversion',
             'maxwell_source_bound_conversion_evidence': 'owned_source_conversion',
             'maxwell_adopted_conversion_evidence': 'owned_adoption',
             'maxwell_reused_conversion_evidence': 'owned_reuse'}
    require(kind in basis and admission['basis'] == basis[kind], 'Unsupported native scientific origin')
    if kind != 'maxwell_reused_conversion_evidence':
        require(json_equal(admission['direct_origin'], dict(kind='modular',
            envelope=details['native_artifacts'], stage_id=stage['id'])),
            'Native completion lost its direct scientific origin')
    if kind == 'maxwell_conversion_evidence':
        require(completed_phases(report, details['settings']), 'Local native science is incomplete')
    else:
        require(report.get('evidence_digest') == digest(
            {k: v for k, v in report.items() if k != 'evidence_digest'}), 'Changed native scientific evidence')
        if kind == 'maxwell_source_bound_conversion_evidence':
            require(report.get('scientific_execution_complete') is True
                    and completed_phases(report.get('science'), details['settings']),
                    'Source-bound native science is incomplete')
            bound = report['science']
        else:
            bound = report
            require(report.get('scientific_execution_complete') is (kind == 'maxwell_adopted_conversion_evidence'),
                    'Native completion misstates new scientific execution')
            if kind == 'maxwell_adopted_conversion_evidence':
                require(report.get('durable_publication_verified') is True, 'Adoption publication is incomplete')
            else:
                require(report.get('original_scientific_execution_complete') is True
                        and json_equal(report.get('origin'), admission['direct_origin']),
                        'Reuse lost its direct scientific origin')
        for key, expected in dict(owner=stage['owner'], unit_id=details['native_unit']['unit_id'],
                settings=stage['settings'], stage_signature=stage['signature'],
                artifacts=admission['artifacts']).items():
            require(json_equal(bound.get(key), expected), 'Native evidence lost its ' + key + ' binding')


def read_json(path):
    with Path(path).open('rb') as stream:
        return _api().decode_json(stream.read(_api().MAX_JSON_BYTES + 1))


def inventory(root):
    result = {}
    require(root.is_dir() and not root.is_symlink(), 'Local unit must be a real directory')
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'Local conversion units cannot contain symlinks')
        require(path.is_file() or path.is_dir(), 'Local conversion dependencies must be regular files')
        if path.is_file():
            with path.open('rb') as stream:
                result[path.relative_to(root).as_posix()] = dict(size=path.stat().st_size,
                    sha256=hashlib.file_digest(stream, 'sha256').hexdigest())
    return result


def verify_artifacts(actual, details):
    for kind, ref in details['artifacts'].items():
        require(kind in NAMES and json_equal(actual.get(NAMES[kind]),
                dict(size=ref['size'], sha256=ref['sha256'])),
                'Local sibling differs from its native published object')


def verify_identity(root, expected=None, *, common_code=None, unit_meta=None, artifacts_first=False):
    """Verify a relocated unit using only its declared local dependencies."""
    load_contracts(common_code)
    root = Path(root).absolute()
    receipt = read_json(root/'conversion-unit.json')
    schema_one(receipt, 'local conversion receipt', 'mea_maxwell_local_unit')
    require(receipt.get('digest') == digest({k: v for k, v in receipt.items() if k != 'digest'}),
            'Invalid local conversion receipt')
    require(receipt.get('document_type') == 'mea_maxwell_local_unit' and receipt.get('complete') is True,
            'Local conversion is incomplete')
    actual = inventory(root)
    actual.pop('conversion-unit.json')
    details = receipt['unit_meta']['maxwell_conversion']
    schema_one(details, 'local conversion metadata')
    if not artifacts_first:
        require(json_equal(actual, receipt['files']), 'Local conversion dependency inventory changed')
    verify_artifacts(actual, details)
    if artifacts_first:
        require(json_equal(actual, receipt['files']), 'Local conversion dependency inventory changed')
    if unit_meta is not None:
        require(isinstance(unit_meta, dict) and all(
            key in unit_meta and json_equal(unit_meta[key], value)
            for key, value in receipt['unit_meta'].items()),
            'Stage unit metadata differs from its native conversion receipt')
    def frozen(relative, ref):
        require(json_equal(actual.get(relative), dict(size=ref['size'], sha256=ref['sha256'])),
                'Local provenance differs from its native published object')
        return read_json(root/relative)
    envelope = frozen('conversion/native-artifacts.json', details['native_artifacts'])
    reference = details['native_artifacts']
    native_transport = 's3' if isinstance(reference.get('uri'), str) and reference['uri'].startswith('s3://') else 'local'
    _api().validate_ref(reference, strong=True, transport=native_transport)
    validate_envelope(envelope, transport=native_transport)
    native_ref = selected_stage(envelope, 'conversion', transport=native_transport)
    stage = frozen('conversion/native-stage.json', native_ref['manifest'])
    validate_stage(stage, transport=native_transport)
    require(json_equal(stage['owner'], details['native_owner'])
            and json_equal(stage['settings'], details['settings']),
            'Local native ownership or settings changed')
    require(any(json_equal(details['native_unit'], unit) for unit in stage['unit_inputs']),
            'Local unit lost its explicit native binding')
    frozen('conversion/recordings.json', envelope['selection'])
    require(json_equal(read_json(root/'conversion/native-unit.json'), details['native_unit']), 'Native unit provenance changed')
    outcomes = [o for o in stage['outcomes'] if json_equal(o['unit_id'], details['native_unit']['unit_id'])]
    require(len(outcomes) == 1 and outcomes[0]['status'] == 'complete'
            and json_equal(outcomes[0]['evidence'], details['native_evidence'])
            and json_equal({a['kind']: a['object'] for a in outcomes[0]['artifacts']}, details['artifacts']),
            'Local conversion lost its complete native outcome')
    report = frozen('conversion/native-evidence.json', details['native_evidence'])
    verify_native_admission(report, details, stage, native_ref)
    if 'projection' in details['native_unit']:
        frozen('conversion/projection.json', details['native_unit']['projection'])
    if expected is not None:
        require(receipt['plan_digest'] == expected[0] and json_equal(receipt['unit_meta'], expected[1]['unit_meta']),
                'Local unit differs from its frozen bridge plan')
        row = expected[1]
        require(set(actual) == set(row['files']) | set(row['evidence']), 'Local dependency coverage differs from the plan')
        for relative, ref in row['files'].items():
            require(json_equal(actual[relative], dict(size=ref['size'], sha256=ref['sha256'])), 'Changed frozen local dependency')
        for relative, value in row['evidence'].items():
            require((root/relative).read_bytes() == canonical_json(value), 'Changed native provenance copy')
    return receipt
