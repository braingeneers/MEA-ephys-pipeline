#!/usr/bin/env python3
"""Exercise owning-manager configuration and complete backend writes, offline.

Inputs are copied read-only native/core fixtures. Outputs need a fresh directory.
This focused compatibility check complements the real scientific writer pilot.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

import numpy as np
from hdmf.container import AbstractContainer, Data
from hdmf.data_utils import DataIO
from hdmf_zarr import NWBZarrIO
from pynwb import NWBFile, NWBHDF5IO, TimeSeries, validate
from pynwb.ecephys import ElectricalSeries, LFP
from neuroconv.tools.nwb_helpers import configure_backend, get_default_backend_configuration
import neuroconv.tools.nwb_helpers._dataset_configuration as discovery

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'capsules/nwb_common/code'),
                str(ROOT / 'pipeline/components/maxwell/runtime')]
from source_nwb import open_source_nwb


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def inventory(path):
    paths = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
    return {str(p.relative_to(path) if path.is_dir() else p.name): sha(p) for p in paths}


def metadata_graph(nwb):
    """Retain all authored/model fields and canonical references outside signals."""
    # Builder discovery accesses these optional core collections, materializing
    # empty LabelledDicts. Access the same public getters on both sides; retain
    # every collection and its complete contents rather than dropping fields.
    for field in ('analysis', 'device_models', 'events', 'icephys_electrodes',
                  'imaging_planes', 'intervals', 'lab_meta_data', 'ogen_sites', 'scratch',
                  'stimulus', 'stimulus_template'):
        if hasattr(nwb, field):
            getattr(nwb, field)
    nodes = {}

    def normalize(value):
        if isinstance(value, AbstractContainer):
            identifier = value.object_id
            if identifier not in nodes:
                nodes[identifier] = {}
                fields = {}
                for key, item in value.fields.items():
                    if value is nwb and key in ('acquisition', 'processing', 'units'):
                        continue
                    # Newly authored HDMF columns start as lists; backend
                    # configuration converts them to arrays before writing.
                    # Compare the complete dataset values on either side.
                    if isinstance(value, Data) and key == 'data':
                        item = data(item)
                        if isinstance(item, (list, tuple)):
                            item = np.asarray(item)
                    fields[key] = normalize(item)
                kind = getattr(value, 'neurodata_type', getattr(value, 'data_type', type(value).__name__))
                nodes[identifier] = dict(name=value.name, namespace=value.namespace,
                                         neurodata_type=kind, fields=fields)
            return {'reference': identifier}
        if isinstance(value, DataIO):
            return normalize(value.data)
        if isinstance(value, np.ndarray):
            if value.dtype.kind in 'biufc':
                array = value.copy() if value.dtype.kind in 'fc' else value
                if value.dtype.kind in 'fc':
                    array[np.isnan(array)] = np.nan
                return dict(shape=list(array.shape), dtype=array.dtype.str,
                            sha256=hashlib.sha256(array.tobytes()).hexdigest())
            return dict(shape=list(value.shape), values=normalize(value.tolist()))
        if isinstance(value, np.generic):
            return normalize(value.item())
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if isinstance(value, datetime):
            return {'datetime': value.isoformat()}
        if isinstance(value, bytes):
            return value.decode('utf-8')
        if hasattr(value, 'shape') and hasattr(value, '__getitem__'):
            return normalize(np.asarray(value[()] if value.shape == () else value[:]))
        if isinstance(value, float):
            return {'float_hex': value.hex()}
        if value is None or isinstance(value, (str, bool, int)):
            return value
        raise TypeError('Unsupported metadata value: ' + str(type(value)))

    normalize(nwb)
    return nodes


def add_probe(nwb, name):
    if nwb.electrodes is None:
        device = nwb.create_device('backend-probe-device')
        group = nwb.create_electrode_group('backend-probe-group', 'Probe', 'Fixture', device)
        for index in range(4):
            nwb.add_electrode(id=index, group=group, location='Fixture', x=float(index), y=0.0, z=0.0)
    values = np.arange(256, dtype=np.int16).reshape(64, 4)
    timestamps = np.arange(64, dtype=np.float64) / 1250.0 + 0.0002
    region = nwb.create_electrode_table_region([0, 1, 2, 3], 'Probe channels')
    series = ElectricalSeries(name=name, data=values, electrodes=region, timestamps=timestamps,
                              conversion=6.3e-6, description='Backend configuration regression')
    lfp = LFP(electrical_series=series)
    module = nwb.create_processing_module(name, 'Backend manager regression')
    module.add(lfp)
    return values, timestamps


def arrays_equal(left, right):
    x, y = np.asarray(left), np.asarray(right)
    assert x.dtype == y.dtype and x.shape == y.shape and x.tobytes() == y.tobytes()


def data(value):
    while isinstance(value, DataIO):
        value = value.data
    return value


def cached_schemas(path, consolidated=True):
    attrs = json.loads((path / '.zattrs').read_text())
    pointer = attrs.get('.specloc')
    assert isinstance(pointer, str) and (path / pointer).is_dir(), 'Missing physical schema cache'
    if not consolidated:
        assert not (path / '.zmetadata').exists(), 'Explicit unconsolidated export was overridden'
        return
    metadata = json.loads((path / '.zmetadata').read_text())['metadata']
    assert metadata.get('.zattrs') == attrs, 'Stale consolidated root/schema metadata'
    expected = {str(item.relative_to(path)): json.loads(item.read_text())
                for item in (path / pointer).rglob('*') if item.name in ('.zattrs', '.zarray', '.zgroup')}
    actual = {key: value for key, value in metadata.items() if key.startswith(pointer.rstrip('/') + '/')}
    assert expected and expected == actual, 'Incomplete consolidated schema inventory'


def roundtrip(source, label, backend, output, metadata_only=False, consolidated=True):
    cls = NWBZarrIO if source.is_dir() else NWBHDF5IO
    before_files = inventory(source)
    target = output / (label + ('.zarr' if backend == 'zarr' else '.nwb'))
    target_cls = NWBZarrIO if backend == 'zarr' else NWBHDF5IO
    factory = discovery.get_manager
    with open_source_nwb(source, 'r', cls, for_writing=True) as reader:
        nwb = reader.read()
        assert nwb.read_io.manager is reader.manager
        originals = {name: series for name, series in nwb.acquisition.items()}
        if metadata_only:
            for name in list(nwb.acquisition):
                nwb.acquisition.pop(name)
            nwb.set_modified()
        expected_values, expected_times = add_probe(nwb, label)
        expected_graph = metadata_graph(nwb)
        config = get_default_backend_configuration(nwbfile=nwb, backend=backend)
        paths = config.dataset_configurations
        assert any(path.endswith(label + '/data') for path in paths)
        assert any(path.endswith(label + '/timestamps') for path in paths)
        assert 'electrodes/location/data' in paths
        assert paths['electrodes/location/data'].object_id == nwb.electrodes['location'].object_id
        configure_backend(nwbfile=nwb, backend_configuration=config)
        configured_graph = metadata_graph(nwb)
        if configured_graph != expected_graph:
            (output / (label + '-before.json')).write_text(json.dumps(expected_graph, indent=2) + '\n')
            (output / (label + '-configured.json')).write_text(json.dumps(configured_graph, indent=2) + '\n')
        assert configured_graph == expected_graph
        assert discovery.get_manager is factory
        with target_cls(str(target), 'w') as writer:
            # Cross-backend HDF5 export also requires explicit copying.
            options = {'link_data': False}
            if backend == 'zarr':
                options['consolidate_metadata'] = consolidated
            writer.export(src_io=reader, nwbfile=nwb, write_args=options)
        if backend == 'zarr':
            cached_schemas(target, consolidated)
        with open_source_nwb(target, 'r', target_cls, for_writing=True) as reader_out:
            actual = reader_out.read()
            assert metadata_graph(actual) == expected_graph
            assert not validate(io=reader_out)
            assert set(actual.acquisition) == (set() if metadata_only else set(originals))
            probe = actual.processing[label]['LFP'].electrical_series[label]
            arrays_equal(probe.data[:], expected_values)
            arrays_equal(probe.timestamps[:], expected_times)
            assert probe.electrodes.table is actual.electrodes
            arrays_equal(probe.electrodes.data[:], np.arange(4, dtype=np.int64))
            if not metadata_only:
                for name, series in originals.items():
                    a, b = data(series.data), actual.acquisition[name].data
                    assert a.shape == b.shape and a.dtype == b.dtype
                    for start in range(0, a.shape[0], 60000):
                        arrays_equal(a[start:start + 60000], b[start:start + 60000])
    assert inventory(source) == before_files
    return dict(source=str(source), output=str(target), backend=backend, metadata_only=metadata_only,
                complete_metadata_nodes=len(expected_graph), configured_datasets=len(paths), source_unchanged=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', type=Path, action='append', required=True)
    parser.add_argument('--core', type=Path, action='append', default=[])
    parser.add_argument('--bad', type=Path, action='append', default=[])
    parser.add_argument('--stale', type=Path, help='Read-only pre-fix Zarr export for actual stale-cache rejection')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expect-original-failure', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.expect_original_failure:
        failures = []
        for source in args.native:
            before = inventory(source)
            with open_source_nwb(source, 'r', NWBHDF5IO, for_writing=True) as reader:
                try:
                    get_default_backend_configuration(nwbfile=reader.read(), backend='zarr')
                except ValueError as error:
                    assert str(error) == "Could not find location 'electrodes/location/data' in builder (location is missing)."
                    failures.append(str(source))
                else:
                    raise AssertionError('Original foreign-manager failure did not reproduce')
            assert inventory(source) == before
        (args.output / 'result.json').write_text(json.dumps(dict(passed=True, expected_original_failures=failures), indent=2) + '\n')
        return
    records = []
    for index, source in enumerate(args.native):
        full = roundtrip(source, 'native_' + str(index), 'zarr', args.output)
        records.append(full)
        records.append(roundtrip(source, 'metadata_' + str(index), 'zarr', args.output, True))
        records.append(roundtrip(Path(full['output']), 'zarr_source_' + str(index), 'hdf5', args.output, True))
    for index, source in enumerate(args.core):
        records.append(roundtrip(source, 'core_' + str(index), 'hdf5' if source.is_dir() else 'zarr', args.output))
    records.append(roundtrip(args.native[0], 'unconsolidated', 'zarr', args.output, True, False))
    fresh = NWBFile('In-memory manager fallback', 'fresh-manager', datetime(2026, 1, 1, tzinfo=timezone.utc))
    values, times = add_probe(fresh, 'in_memory')
    assert fresh.read_io is None
    graph = metadata_graph(fresh)
    configuration = get_default_backend_configuration(nwbfile=fresh, backend='hdf5')
    configure_backend(nwbfile=fresh, backend_configuration=configuration)
    with NWBHDF5IO(str(args.output / 'in-memory.nwb'), 'w') as writer:
        writer.write(fresh)
    with open_source_nwb(args.output / 'in-memory.nwb', 'r', NWBHDF5IO) as reader:
        nwb = reader.read()
        assert metadata_graph(nwb) == graph and not validate(io=reader)
        arrays_equal(nwb.processing['in_memory']['LFP'].electrical_series['in_memory'].data[:], values)
    with ExitStack() as stack:
        readers = [stack.enter_context(open_source_nwb(source, 'r', NWBHDF5IO)) for source in args.native]
        nwbs = [reader.read() for reader in readers]
        assert readers[0].manager is not readers[1].manager
        graphs = [metadata_graph(nwb) for nwb in nwbs]
        configs = [get_default_backend_configuration(nwbfile=nwb, backend='zarr') for nwb in nwbs]
        for nwb, configuration in zip(nwbs, configs):
            assert configuration.dataset_configurations['electrodes/location/data'].object_id == nwb.electrodes['location'].object_id
            configure_backend(nwbfile=nwb, backend_configuration=configuration)
        assert [metadata_graph(nwb) for nwb in nwbs] == graphs
    rejected = []
    for source in args.bad:
        before = inventory(source)
        try:
            with open_source_nwb(source, 'r', NWBHDF5IO, for_writing=True):
                pass
        except ValueError as error:
            assert 'NWB write blocked' in str(error)
            rejected.append(str(source))
        else:
            raise AssertionError('Unwritable old graph accepted')
        assert inventory(source) == before
    sys.path.insert(0, str(ROOT / 'containers/nwb-backend'))
    from patch_runtime import rewrite, BEFORE, AFTER, CACHE_BEFORE, CACHE_AFTER
    locks = json.loads((ROOT / 'containers/nwb-backend/patch.lock.json').read_text())['patches']
    for lock, before, after in zip(locks, (BEFORE, CACHE_BEFORE), (AFTER, CACHE_AFTER)):
        module = Path(importlib.metadata.distribution(lock['distribution']).locate_file(lock['module']))
        assert sha(module) == lock['patched_sha256']
        original = module.read_bytes().replace(after, before)
        assert rewrite(original, lock['version'], lock, before, after) == module.read_bytes()
        for changed, version in ((original + b'\n', lock['version']), (original, 'unexpected')):
            try:
                rewrite(changed, version, lock, before, after)
            except ValueError:
                pass
            else:
                raise AssertionError('Unreviewed dependency patch accepted')
    stale_rejected = False
    if args.stale:
        before = inventory(args.stale)
        try:
            cached_schemas(args.stale)
        except AssertionError as error:
            assert 'Stale consolidated' in str(error)
            stale_rejected = True
        else:
            raise AssertionError('Actual stale consolidated cache accepted')
        assert inventory(args.stale) == before
    report = dict(passed=True, complete_roundtrips=records, in_memory_fallback=True,
                  two_invocation_managers_isolated=True, bad_graphs_rejected=rejected,
                  patch_drift_controls=4, stale_cache_rejected=stale_rejected, scientific_producer_clis=0)
    (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'complete_roundtrips'}, indent=2))


if __name__ == '__main__':
    main()
