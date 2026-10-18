"""Read old cell-culture layouts in isolation and migrate task-local copies.

The reader deliberately never imports ndx_cell_culture. Cached namespaces are
validated in a fresh process; HDF5 references are resolved before old groups are
replaced. Acquisition and processing datasets are retained byte for byte.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import h5py

MIGRATION_SCRATCH = 'cell_culture_v1_migration'
MIGRATION_VERSION = 1
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024

FIELDS = {
    'CellLine': ('cell_line_id', 'cell_line_type', 'sample_label', 'species', 'cell_source_type', 'cell_line_name', 'cell_line_rrid', 'clone_id', 'clonal_status', 'passage_number', 'disease_or_diagnosis', 'reference_genome', 'notes'),
    'CellCulture': ('culture_id', 'culture_type', 'sample_label', 'species', 'culture_subtype', 'batch_label', 'age', 'age_reference', 'disease_or_diagnosis', 'reference_genome', 'notes'),
    'GeneticVariant': ('variant_id', 'target_symbol', 'target_coordinates', 'edit_type', 'edit_description', 'origin', 'method', 'delivery_method', 'zygosity_or_edit_state', 'validation_status', 'validation_method', 'notes'),
    'ConstructApplication': ('application_id', 'source', 'delivery_route', 'delivery_mechanism', 'vector_type', 'payload', 'promoter', 'genomic_persistence_state', 'application_time_relative_to_recording', 'age_modified', 'age_modified_reference', 'expression_status', 'validation_method', 'notes'),
    'CultureProtocol': ('protocol_id', 'protocol_name', 'protocol_uri', 'protocol_doi', 'protocol_version', 'patterning_summary', 'media_summary', 'notes'),
    'ExperimentContext': ('experiment_id', 'session_id', 'age_at_recording', 'age_reference', 'recording_platform', 'media_or_bath', 'experimental_setup', 'notes', 'temperature_c', 'recording_duration_s', 'spontaneous_activity', 'electrical_stimulation', 'optical_stimulation', 'pharmacology_present'),
    'Pharmacology': ('pharmacology_id', 'agent', 'concentration', 'concentration_unit', 'purpose', 'notes', 'start_time_s', 'end_time_s'),
}
SUBJECT_FIELDS = ('subject_id', 'age', 'description', 'genotype', 'sex', 'species', 'strain', 'weight', 'date_of_birth')


def plain(value):
    if isinstance(value, bytes):
        return value.decode('utf-8')
    if hasattr(value, 'tolist'):
        return plain(value.tolist())
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if hasattr(value, 'item'):
        return plain(value.item())
    return value


def cell_culture_layout(path):
    """Inspect the schema and stored types; release labels are insufficient."""
    with h5py.File(path, 'r') as handle:
        legacy = []
        contexts = []
        def visit(name, obj):
            if isinstance(obj, h5py.Group) and plain(obj.attrs.get('namespace', '')) == 'ndx-cell-culture':
                kind = plain(obj.attrs.get('neurodata_type', ''))
                if kind == 'CultureExperimentContext':
                    legacy.append('/' + name)
                elif kind == 'ExperimentContext':
                    contexts.append('/' + name)
        handle.visititems(visit)
        specs = handle.get('specifications/ndx-cell-culture')
        old_schema = False
        if specs is not None:
            for version in specs.values():
                for key, item in version.items():
                    if isinstance(item, h5py.Dataset) and 'extensions' in key:
                        value = plain(item[()])
                        old_schema |= 'CultureExperimentContext' in value
        if legacy or old_schema:
            return {'layout': 'legacy', 'context_paths': legacy}
        if contexts:
            return {'layout': 'current', 'context_paths': contexts}
        return {'layout': 'core_or_biology', 'context_paths': []}


def _attrs(group, kind, links=()):
    unknown = set(group.attrs) - set(FIELDS[kind]) - {'namespace', 'neurodata_type', 'object_id'}
    if unknown:
        raise ValueError(f'Unsupported legacy {kind} attributes at {group.name}: {sorted(unknown)}')
    return {name: plain(group.attrs[name]) for name in FIELDS[kind] if name in group.attrs}


def extract_legacy(path):
    # Validation uses only the embedded namespace in this isolated process.
    from pynwb import validate
    errors = validate(path=str(path))
    if errors:
        raise ValueError('Legacy NWB fails its cached schema validation; correct it before migration')
    with h5py.File(path, 'r') as handle:
        subject = handle.get('general/subject')
        if subject is None or plain(subject.attrs.get('neurodata_type', '')) != 'CellCultureSubject':
            raise ValueError('Legacy context requires a CellCultureSubject for lossless migration')
        if set(subject.attrs) - {'namespace', 'neurodata_type', 'object_id'}:
            raise ValueError('Unsupported legacy subject attributes; refusing metadata loss')
        biology = {'CellLine': {}, 'CellCulture': {}}
        for name, obj in subject.items():
            if isinstance(obj, h5py.Group):
                kind = plain(obj.attrs.get('neurodata_type', ''))
                if kind in biology and name != 'culture':
                    biology[kind][name] = obj
        def references(group, name, target):
            if name not in group:
                return []
            obj = group[name]
            if isinstance(obj, h5py.Group):
                values = [obj]
            else:
                refs = obj[()]
                values = [handle[ref] for ref in ([refs] if isinstance(refs, h5py.Reference) else refs)]
            keys = []
            for obj in values:
                matches = [key for key, candidate in biology[target].items() if candidate.id == obj.id]
                if len(matches) != 1:
                    raise ValueError(f'Ambiguous legacy provenance reference at {group.name}/{name}')
                keys.append(matches[0])
            return keys
        def child(group, kind):
            record = _attrs(group, kind)
            allowed_children = set()
            if kind in ('CellLine', 'CellCulture'):
                record['genetic_variants'] = []
                record['construct_applications'] = []
                for name, obj in group.items():
                    if isinstance(obj, h5py.Group):
                        child_kind = plain(obj.attrs.get('neurodata_type', ''))
                        if child_kind in ('GeneticVariant', 'ConstructApplication', 'CultureProtocol'):
                            allowed_children.add(name)
                            value = child(obj, child_kind)
                            if child_kind == 'CultureProtocol':
                                record['culture_protocol'] = value
                            else:
                                record['genetic_variants' if child_kind == 'GeneticVariant' else 'construct_applications'].append(value)
                if kind == 'CellLine':
                    parents = references(group, 'parent_cell_line', 'CellLine')
                    if len(parents) > 1:
                        raise ValueError(f'{group.name}: multiple parent cell lines require author correction')
                    if parents:
                        record['parent_cell_line_id'] = parents[0]
                    allowed_children.add('parent_cell_line')
                else:
                    record['source_line_ids'] = references(group, 'source_lines', 'CellLine')
                    record['parent_culture_ids'] = references(group, 'parent_cultures', 'CellCulture')
                    allowed_children.update(('source_lines', 'parent_cultures'))
            elif kind == 'GeneticVariant' and 'related_application' in group:
                application = group['related_application']
                record['related_application_id'] = plain(application.attrs['application_id'])
                owners = [(kind, key) for kind, rows in biology.items() for key, owner in rows.items()
                          if any(isinstance(obj, h5py.Group) and plain(obj.attrs.get('neurodata_type', '')) == 'ConstructApplication' and obj.id == application.id for obj in owner.values())]
                if len(owners) != 1:
                    raise ValueError('Ambiguous legacy construct application ownership')
                kind, key = owners[0]
                record['related_application_owner'] = {'collection': 'cell_lines' if kind == 'CellLine' else 'cultures', 'id': key}
                allowed_children.add('related_application')
            elif kind == 'Pharmacology':
                allowed_children.add('experiment')
            unknown = set(group) - allowed_children
            if unknown:
                raise ValueError(f'Unsupported legacy {kind} fields at {group.name}: {sorted(unknown)}')
            return record
        result = {'projection_schema_version': 1,
                  'cell_lines': {key: child(obj, 'CellLine') for key, obj in biology['CellLine'].items()},
                  'cultures': {key: child(obj, 'CellCulture') for key, obj in biology['CellCulture'].items()},
                  'subject': {name: plain(subject[name][()]) for name in SUBJECT_FIELDS if name in subject},
                  'context': {}}
        if 'age' in subject and 'reference' in subject['age'].attrs:
            result['subject']['age__reference'] = plain(subject['age'].attrs['reference'])
        unknown_subject = set(subject) - set(SUBJECT_FIELDS) - set(biology['CellLine']) - set(biology['CellCulture']) - {'culture'}
        if unknown_subject:
            raise ValueError(f'Unsupported legacy subject fields: {sorted(unknown_subject)}')
        culture = references(subject, 'culture', 'CellCulture')
        if len(culture) != 1:
            raise ValueError('Legacy subject must identify exactly one recorded culture')
        result['recorded_culture_id'] = result['subject']['culture_id'] = culture[0]
        wrappers = []
        for obj in handle['general'].values():
            if isinstance(obj, h5py.Group) and plain(obj.attrs.get('neurodata_type', '')) == 'CultureExperimentContext':
                wrappers.append(obj)
        if not wrappers:
            result["_legacy_context_paths"] = []
            result["context"] = {"_legacy_context_absent": True, "_legacy_device_name": ""}
            return result
        if len(wrappers) != 1:
            raise ValueError('Legacy migration supports exactly one recorded context; ambiguous contexts require correction')
        wrapper = wrappers[0]
        if set(wrapper.attrs) - {'namespace', 'neurodata_type', 'object_id'}:
            raise ValueError('Unsupported legacy wrapper attributes; refusing metadata loss')
        contexts = [obj for obj in wrapper.values() if isinstance(obj, h5py.Group) and plain(obj.attrs.get('neurodata_type', '')) == 'ExperimentContext']
        if len(contexts) != 1:
            raise ValueError('Legacy wrapper must contain one experiment context')
        context = contexts[0]
        result['context'] = _attrs(context, 'ExperimentContext')
        if result['context'].get('session_id') and 'general/session_id' in handle and result['context']['session_id'] != plain(handle['general/session_id'][()]):
            raise ValueError('Legacy context and file disagree about session identity; author correction required')
        if 'subject' in context and context['subject'].id != subject.id:
            raise ValueError('Legacy context and file disagree about their subject')
        context_cultures = references(context, 'culture', 'CellCulture')
        if len(context_cultures) != 1:
            raise ValueError('Legacy context must identify one recorded culture')
        # An explicit recording preparation override becomes the effective
        # subject culture; the usual preparation remains in the retained graph.
        result['recorded_culture_id'] = context_cultures[0]
        device_names = [key for key, obj in handle.get('general/devices', {}).items()
                        if isinstance(obj, h5py.Group) and 'device' in context and obj.id == context['device'].id]
        if 'device' in context and len(device_names) != 1:
            raise ValueError('Ambiguous legacy device link')
        result['context']['_legacy_device_name'] = device_names[0] if device_names else ''
        if set(context) - {'subject', 'culture', 'device'}:
            raise ValueError('Unsupported legacy context child metadata')
        result['context']['pharmacology'] = []
        for obj in wrapper.values():
            if obj.id == context.id:
                continue
            if plain(obj.attrs.get('neurodata_type', '')) != 'Pharmacology':
                raise ValueError('Unsupported legacy lab metadata in context wrapper')
            if 'experiment' not in obj or obj['experiment'].id != context.id:
                raise ValueError('Legacy pharmacology references a different experiment')
            result['context']['pharmacology'].append(child(obj, 'Pharmacology'))
        result['_legacy_context_paths'] = [wrapper.name]
        return result


def read_legacy_snapshot(path):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--extract', str(path)], capture_output=True)
    if result.returncode:
        # Subprocess diagnostics contain paths/field names, never credentials.
        raise ValueError('Legacy v1.0.0 migration could not preserve source metadata: ' + result.stderr.decode()[-2000:])
    if len(result.stdout) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Legacy metadata exceeds the bounded migration snapshot limit')
    return json.loads(result.stdout)


def merge_legacy_projection(legacy, authored):
    """Absent fields retain source values; supplied lists express replacement."""
    result = copy.deepcopy(legacy)
    for key, value in authored.items():
        if key not in ('cell_lines', 'cultures', 'subject', 'context'):
            result[key] = copy.deepcopy(value)
    maps = {}
    for collection, identity in (('cell_lines', 'cell_line_id'), ('cultures', 'culture_id')):
        existing = result[collection]
        mapping = {}
        for key, record in (authored.get(collection) or {}).items():
            matches = [old for old, value in existing.items() if value.get(identity) == record.get(identity, key)]
            if key in existing:
                old_key = key
            elif len(matches) == 1:
                old_key = matches[0]
            elif len(matches) > 1:
                raise ValueError(f'Ambiguous legacy {collection} identity {key}; author correction required')
            else:
                old_key = key
            base = copy.deepcopy(existing.get(old_key, {}))
            for field, value in record.items():
                if field in ('genetic_variants', 'construct_applications') and isinstance(value, list):
                    id_field = 'variant_id' if field == 'genetic_variants' else 'application_id'
                    prior = {row[id_field]: row for row in base.get(field, [])}
                    base[field] = []
                    for row in value:
                        merged_row = {**prior.get(row.get(id_field), {}), **copy.deepcopy(row)}
                        if 'related_application_id' in row and 'related_application_owner' not in row:
                            merged_row.pop('related_application_owner', None)
                        base[field].append(merged_row)
                else:
                    base[field] = copy.deepcopy(value)
            if old_key != key:
                existing.pop(old_key, None)
            existing[key] = base
            mapping[old_key] = key
        maps[collection] = mapping
    for owner in result['cell_lines'].values():
        key = owner.get('parent_cell_line_id')
        if key:
            owner['parent_cell_line_id'] = maps['cell_lines'].get(key, key)
    for owner in result['cultures'].values():
        for field, collection in (('source_line_ids', 'cell_lines'), ('parent_culture_ids', 'cultures')):
            owner[field] = [maps[collection].get(key, key) for key in owner.get(field, [])]
    for records in (result['cell_lines'], result['cultures']):
        for record in records.values():
            for variant in record.get('genetic_variants', []):
                owner = variant.get('related_application_owner')
                if isinstance(owner, dict):
                    owner['id'] = maps[owner['collection']].get(owner['id'], owner['id'])
    for key in ('subject', 'context'):
        if isinstance(authored.get(key), dict):
            result[key].update(copy.deepcopy(authored[key]))
    supplied_context = authored.get('context') or {}
    conditions = set(FIELDS['ExperimentContext']) - {'session_id', 'experiment_id'} | {'pharmacology'}
    if any(key in supplied_context and supplied_context[key] not in (None, '', [], {}) for key in conditions):
        result['context'].pop('_legacy_context_absent', None)
    result['recorded_culture_id'] = maps['cultures'].get(result.get('recorded_culture_id'), result.get('recorded_culture_id'))
    if result['subject'].get('culture_id'):
        result['subject']['culture_id'] = maps['cultures'].get(result['subject']['culture_id'], result['subject']['culture_id'])
    from cell_culture_projection import validate_projection_export
    validate_projection_export(result)
    return result


def retained_legacy_projection(nwbfile):
    scratch = getattr(nwbfile, 'scratch', {}).get(MIGRATION_SCRATCH)
    if scratch is None:
        return None
    value = plain(scratch.data)
    state = json.loads(value)
    if state.get('version') != MIGRATION_VERSION:
        raise ValueError('Unsupported cell-culture migration provenance')
    return state['legacy_projection']


def retained_projection(nwbfile, authored):
    legacy = retained_legacy_projection(nwbfile)
    return merge_legacy_projection(legacy, authored) if legacy is not None else authored


def _dataset_identity(handle):
    result = {}
    for root in ('acquisition', 'processing', 'stimulus', 'units', 'intervals', 'general/devices', 'general/extracellular_ephys'):
        if root not in handle:
            continue
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset):
                result[root + '/' + name] = (obj.shape, obj.dtype.str, obj.id.get_storage_size(), obj.id.get_offset(), h5py.h5o.get_info(obj.id).addr)
        handle[root].visititems(visit)
    return result


def metadata_graph_paths(handle, removed_paths):
    """Include HDF5 expand_refs root aliases to these exact model objects.

    Ordinary links/references outside the graph remain foreign. The reserved
    aliases are accepted only when their target already belongs to this graph.
    """
    addresses = set()
    for path in removed_paths:
        if path not in handle:
            continue
        group = handle[path]
        addresses.add(h5py.h5o.get_info(group.id).addr)
        def collect(name, obj):
            if isinstance(obj, h5py.Dataset) or plain(obj.attrs.get('namespace', '')) == 'ndx-cell-culture':
                addresses.add(h5py.h5o.get_info(obj.id).addr)
        group.visititems(collect)
    return list(removed_paths) + ['/' + name for name in handle
        if re.fullmatch(r'~obj_pointed_by_[0-9]+', name)
        and h5py.h5o.get_info(handle[name].id).addr in addresses
        and '/' + name not in removed_paths]


def reject_external_metadata_references(handle, removed_paths):
    removed_paths = metadata_graph_paths(handle, removed_paths)
    removed = set()
    for path in removed_paths:
        group = handle[path]
        removed.add(h5py.h5o.get_info(group.id).addr)
        def collect(name, obj):
            if isinstance(obj, h5py.Dataset) or plain(obj.attrs.get('namespace', '')) == 'ndx-cell-culture':
                removed.add(h5py.h5o.get_info(obj.id).addr)
        group.visititems(collect)
    seen = set()
    def check_ref(value):
        if isinstance(value, h5py.Reference) and value:
            if h5py.h5o.get_info(handle[value].id).addr in removed:
                raise ValueError('Other NWB metadata references the legacy graph; an explicit migration is required')
        elif hasattr(value, 'flat') and getattr(value, 'ndim', 0) > 0:
            for item in value.flat:
                check_ref(item)
    def visit(group):
        addr = h5py.h5o.get_info(group.id).addr
        if addr in seen:
            return
        seen.add(addr)
        for name in group:
            obj = group[name]
            if obj.name in removed_paths:
                continue
            if h5py.h5o.get_info(obj.id).addr in removed:
                raise ValueError('Other NWB metadata links to the legacy graph; refusing to invalidate it')
            for key in obj.attrs:
                check_ref(obj.attrs[key])
            if isinstance(obj, h5py.Group):
                visit(obj)
            elif h5py.check_dtype(ref=obj.dtype):
                check_ref(obj[()])
            elif obj.dtype.names:
                for field in obj.dtype.names:
                    if h5py.check_dtype(ref=obj.dtype[field]):
                        check_ref(obj[field])
    visit(handle)


CORE_FIELD_PATHS = {
    'identifier': 'identifier', 'session_description': 'session_description',
    'session_start_time': 'session_start_time', 'session_id': 'general/session_id',
    **{key: 'general/' + key for key in ('lab', 'institution', 'experiment_description', 'keywords', 'related_publications', 'notes', 'protocol', 'experimenter')},
}


def core_values(nwbfile):
    from datetime import datetime
    result = {}
    for key in CORE_FIELD_PATHS:
        value = plain(getattr(nwbfile, key, None))
        result[key] = value.isoformat() if isinstance(value, datetime) else value
    return result


def write_changed_core_fields(path, before, after):
    # PyNWB append does not replace existing scalar datasets. Apply the callback's
    # explicit core-field changes after closing its append handle.
    with h5py.File(path, 'a') as handle:
        for key, target in CORE_FIELD_PATHS.items():
            if before[key] == after[key]:
                continue
            attrs = dict(handle[target].attrs) if target in handle else {}
            if target in handle:
                del handle[target]
            value = after[key]
            if value is not None:
                handle.create_dataset(target, data=value, dtype=h5py.string_dtype())
                for name, attr in attrs.items():
                    handle[target].attrs[name] = attr


def migrate_legacy_copy(source, destination, authored, apply_projection=None):
    """Create a v1.0.0 copy, retaining source identity and data storage addresses."""
    from cell_culture_projection import apply_cell_culture_projection
    from hdmf.build import BuildManager
    from pynwb import NWBHDF5IO, get_type_map, validate
    import ndx_cell_culture  # Register v1.0.0 only in the writing process.

    snapshot = read_legacy_snapshot(source)
    merged = merge_legacy_projection(snapshot, authored)
    with h5py.File(source, 'r') as handle:
        reject_external_metadata_references(handle, snapshot['_legacy_context_paths'] + ['/general/subject'])
        before = _dataset_identity(handle)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError('Migration destination already exists')
    with tempfile.TemporaryDirectory(prefix='.cell-culture-migration-', dir=destination.parent) as directory:
        staged = Path(directory) / 'staged.nwb'
        shutil.copyfile(source, staged)
        with h5py.File(staged, 'a') as handle:
            for path in metadata_graph_paths(handle,snapshot['_legacy_context_paths'] + ['/general/subject']):
                del handle[path]
            if 'specifications/ndx-cell-culture' in handle:
                del handle['specifications/ndx-cell-culture']
        type_map = get_type_map()
        NWBHDF5IO.load_namespaces(type_map.namespace_catalog, str(staged))
        with NWBHDF5IO(str(staged), 'a', manager=BuildManager(type_map)) as io:
            nwbfile = io.read()
            before_core = core_values(nwbfile)
            # Old wrappers stored a session ID independently of the optional
            # file field. Promote that explicit source fact into the copy so
            # the new file-derived context property retains its identity.
            legacy_session = (snapshot.get('context') or {}).get('session_id')
            if not nwbfile.session_id and legacy_session:
                nwbfile.fields['session_id'] = legacy_session
            (apply_projection or apply_cell_culture_projection)(nwbfile, merged)
            nwbfile.add_scratch(json.dumps({'version': MIGRATION_VERSION, 'legacy_projection': snapshot}, sort_keys=True), name=MIGRATION_SCRATCH,
                                description='Preserved legacy biological metadata used when applying later authoring projections')
            after_core = core_values(nwbfile)
            io.write(nwbfile, cache_spec=True)
        write_changed_core_fields(staged, before_core, after_core)
        with h5py.File(staged, 'r') as handle:
            if before != _dataset_identity(handle):
                raise ValueError('Legacy migration changed scientific data storage')
        if validate(path=str(staged)):
            raise ValueError('Migrated file fails v1.0.0 schema validation')
        from cell_culture_projection import verify_cell_culture_projection
        with NWBHDF5IO(str(staged), "r", load_namespaces=True) as io:
            differences = verify_cell_culture_projection(io.read(), merged)
        if differences:
            raise ValueError("Legacy migration did not preserve modeled metadata: " + ", ".join(differences))
        # The caller performs the complete modeled metadata comparison before
        # any durable publication. Creation below is atomic and never overwrites.
        os.link(staged, destination)
    return merged


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--extract':
        raise SystemExit('Expected --extract NWB_FILE')
    try:
        payload = json.dumps(extract_legacy(Path(sys.argv[2])), sort_keys=True)
        if len(payload.encode()) > MAX_SNAPSHOT_BYTES:
            raise ValueError('Legacy metadata exceeds the bounded migration snapshot limit')
        print(payload)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


def refresh_projection_copy(source, destination, projection, *, replace_file_fields=False, apply_identity=False):
    """Apply authoring metadata to a new file, preserving the source NWB."""
    from cell_culture_projection import apply_cell_culture_projection, verify_cell_culture_projection
    from pynwb import NWBHDF5IO, validate

    def apply(nwb, wanted):
        context, common = wanted.get('context') or {}, wanted.get('common') or {}
        if replace_file_fields and 'common' in projection:
            for field in ('lab', 'institution', 'experiment_description', 'keywords', 'related_publications', 'notes', 'protocol', 'experimenter'):
                nwb.fields.pop(field, None)
        if apply_identity:
            if context.get('session_start_time'):
                from datetime import datetime
                timestamp = datetime.fromisoformat(context['session_start_time'].replace('Z', '+00:00'))
                if timestamp.tzinfo is None:
                    raise ValueError('Recording session start time must include its time zone')
                nwb.fields['session_start_time'] = timestamp
            if context.get('session_id'):
                nwb.fields['session_id'] = context['session_id']
        for field in ('lab', 'institution', 'experiment_description', 'keywords', 'related_publications', 'notes', 'protocol'):
            if common.get(field) not in (None, '', [], {}):
                nwb.fields[field] = common[field]
        if context.get('session_description') or common.get('experiment_description'):
            nwb.fields['session_description'] = context.get('session_description') or common['experiment_description']
        people = [value['name'] for value in (wanted.get('people') or {}).values() if value.get('name')]
        if people:
            nwb.fields['experimenter'] = people
        if isinstance(wanted.get('subject'), dict):
            nwb.fields.pop('subject', None)
            for key in list(nwb.lab_meta_data):
                if getattr(nwb.lab_meta_data[key], 'neurodata_type', None) == 'ExperimentContext':
                    nwb.lab_meta_data.pop(key)
            apply_cell_culture_projection(nwb, wanted)
        nwb.set_modified()

    if cell_culture_layout(source)['layout'] == 'legacy':
        wanted = migrate_legacy_copy(source, destination, projection, apply_projection=apply)
    else:
        import ndx_cell_culture  # Register current types before a core-only file is opened.
        with NWBHDF5IO(str(source), 'r', load_namespaces=True) as io:
            nwb = io.read()
            wanted = retained_projection(nwb, projection)
            apply(nwb, wanted)
            with NWBHDF5IO(str(destination), 'w', manager=io.manager) as output:
                output.export(src_io=io, nwbfile=nwb, write_args={'link_data': False})
    if validate(path=str(destination)):
        raise ValueError('Refreshed file fails NWB schema validation')
    with NWBHDF5IO(str(destination), 'r', load_namespaces=True) as io:
        errors = verify_cell_culture_projection(io.read(), wanted)
        if errors:
            raise ValueError('Metadata refresh failed readback: ' + ', '.join(errors))
    return wanted
