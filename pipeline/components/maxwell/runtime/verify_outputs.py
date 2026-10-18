"""Invoke owned output/model checks and verify every signal LINDI byte range."""
import json
from pathlib import Path
import sys

import h5py
import numpy as np
import ndx_cell_culture  # register exact owned typed extension before readback
from pynwb import NWBHDF5IO
from cell_culture_projection import verify_cell_culture_projection
from ephys_modular.contracts import ContractError, canonical_json, decode_json
from maxwell_lindi_codec import MaxwellMxwDataCodec
from nwb_graph import require_writable_hdf5
from validate_conversion_outputs import (DEFAULT_NWB_DATASET_PATH, prepare_maxwell_plugin,
    validate_lindi, validate_linked_maxwell, validate_nwb_data)


def verify_outputs(request):
    source, nwb, lindi = (Path(request[key]) for key in ('source', 'nwb', 'lindi'))
    raw_path = request['raw_dataset_path']
    plugin = Path(prepare_maxwell_plugin())
    require_writable_hdf5(nwb)
    shape, chunks = validate_nwb_data(original_file=source, nwb_path=nwb,
        raw_dataset_path=raw_path, nwb_dataset_path=DEFAULT_NWB_DATASET_PATH,
        chunk_size_samples=50000)
    if request.get('companion'):
        validate_linked_maxwell(original_file=source, linked_path=Path(request['companion']),
            nwb_path=nwb, raw_dataset_path=raw_path,
            nwb_dataset_path=DEFAULT_NWB_DATASET_PATH, legacy_window_samples=2000)
        # Full linked reads, including windows not sampled by the maintained checker.
        with h5py.File(source) as original, h5py.File(request['companion']) as linked:
            for start in range(0, shape[0], 50000):
                if not np.array_equal(original[raw_path][:, start:start + 50000],
                                      linked[raw_path][:, start:start + 50000]):
                    raise ContractError('Linked companion differs from the selected source')
    validate_lindi(lindi_path=lindi, nwb_path=nwb, expected_shape=shape, expected_chunks=chunks,
        nwb_dataset_path=DEFAULT_NWB_DATASET_PATH, plugin_directory=plugin)
    # A scientific LINDI sidecar can exceed the envelope JSON budget. Keep the
    # maintained reader's behavior; the 32 MiB limit applies to contract metadata.
    with lindi.open() as stream:
        value = json.load(stream)
    if value['generationMetadata']['sourceUrl'] != request['reader_url']:
        raise ContractError('LINDI reader URL differs from the reviewed durable output')
    prefix = DEFAULT_NWB_DATASET_PATH.lstrip('/')
    references, templates = value['refs'], value['templates']
    decoder = MaxwellMxwDataCodec(plugin_path=plugin / 'libcompression.so')
    time_chunks = (shape[0] + chunks[0] - 1) // chunks[0]
    channel_chunks = (shape[1] + chunks[1] - 1) // chunks[1]
    expected_keys = {f'{prefix}/{t}.{c}' for t in range(time_chunks) for c in range(channel_chunks)}
    observed_keys = {key for key in references if key.startswith(prefix + '/')
                     and not key.endswith(('/.zarray', '/.zattrs'))}
    if observed_keys != expected_keys:
        raise ContractError('LINDI signal chunk coverage is incomplete or unexpected')
    with h5py.File(nwb) as handle, nwb.open('rb') as stream:
        dataset = handle[DEFAULT_NWB_DATASET_PATH]
        for t in range(time_chunks):
            for c in range(channel_chunks):
                reference = references[f'{prefix}/{t}.{c}']
                if (not isinstance(reference, list) or len(reference) != 3
                        or type(reference[1]) is not int or type(reference[2]) is not int
                        or reference[1] < 0 or reference[2] <= 0):
                    raise ContractError('Malformed LINDI signal byte reference')
                url = reference[0]
                if isinstance(url, str) and url.startswith('{{') and url.endswith('}}'):
                    url = templates.get(url[2:-2])
                if url != request['reader_url']:
                    raise ContractError('LINDI signal refers to a different source URL')
                stream.seek(reference[1])
                encoded = stream.read(reference[2])
                if len(encoded) != reference[2]:
                    raise ContractError('LINDI signal range extends beyond NWB bytes')
                decoded = np.frombuffer(decoder.decode(encoded), dtype='<u2').reshape(chunks)
                start, channel = t * chunks[0], c * chunks[1]
                data = dataset[start:start + chunks[0], channel:channel + chunks[1]]
                expected = np.zeros(chunks, dtype='<u2')
                expected[:data.shape[0], :data.shape[1]] = data
                if not np.array_equal(decoded, expected):
                    raise ContractError('Native LINDI range differs from the NWB signal')
    if request.get('projection'):
        projection = decode_json(Path(request['projection']).read_bytes())
        with NWBHDF5IO(str(nwb), 'r', load_namespaces=True) as io:
            differences = verify_cell_culture_projection(io.read(), projection)
            if differences:
                raise ContractError('NWB modeled metadata differs from the frozen projection')
    return {'schema_version': 1, 'complete': True, 'signal_shape': list(shape),
        'signal_values_checked': int(np.prod(shape)), 'lindi_signal_ranges_checked': len(expected_keys),
        'all_signal_values_equal': True, 'all_lindi_signal_ranges_equal': True,
        'all_linked_values_equal': True if request.get('companion') else None,
        'modeled_projection_verified': bool(request.get('projection'))}


if __name__ == '__main__':
    request = decode_json(Path(sys.argv[1]).read_bytes())
    Path(sys.argv[2]).write_bytes(canonical_json(verify_outputs(request)))
