#!/usr/bin/env python3
"""Generate a LINDI sidecar file for an NWB file."""

from __future__ import annotations

from workflow_defaults import scientific_default

import argparse
import json
import sys
from pathlib import Path

import h5py
import lindi

from maxwell_lindi_codec import (
    MAXWELL_FILTER_ID,
    MaxwellMxwDataCodec,
    codec_config,
    register_maxwell_mxw_data_codec,
)


def fail(message: str) -> None:
    print(f"[generate_lindi_metadata] {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_optional_int(raw_value: str, *, option_name: str, allow_zero: bool = True) -> int | None:
    normalized = raw_value.strip().lower()
    if normalized in {"none", "null", ""}:
        return None

    try:
        value = int(raw_value)
    except ValueError:
        fail(f"Parameter --{option_name} must be an integer or 'none'.")

    if allow_zero:
        if value < 0:
            fail(f"Parameter --{option_name} must be >= 0 or 'none'.")
    else:
        if value <= 0:
            fail(f"Parameter --{option_name} must be > 0 or 'none'.")

    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nwb-file", required=True, help="Path to local NWB file.")
    parser.add_argument(
        "--output-file",
        required=True,
        help="Output LINDI file path (typically .lindi.json).",
    )
    parser.add_argument(
        "--source-url",
        required=True,
        help=(
            "Remote or local URL/path to embed in references for the NWB file "
            "(for example https://host/bucket/key or /abs/path/file.nwb)."
        ),
    )
    parser.add_argument(
        "--num-dataset-chunks-threshold",
        default=scientific_default('lindi_num_dataset_chunks_threshold'),
        help=(
            "LINDI external-link threshold. Use 'none' to include chunk references "
            "for all chunked datasets; otherwise use integer >= 0."
        ),
    )
    parser.add_argument(
        "--contiguous-dataset-max-chunk-size",
        default=str(scientific_default('lindi_contiguous_dataset_max_chunk_size')),
        help=(
            "Max byte size for synthetic chunks of contiguous datasets. "
            "Use integer > 0 or 'none'."
        ),
    )
    return parser.parse_args()


def _hdf5_filter_ids(dataset: h5py.Dataset) -> list[int]:
    creation_properties = dataset.id.get_create_plist()
    return [
        int(creation_properties.get_filter(index)[0])
        for index in range(creation_properties.get_nfilters())
    ]


def validate_maxwell_filter_masks(nwb_file: Path) -> tuple[int, int]:
    """Require every allocated filter-401 chunk to have actually used the filter."""
    dataset_count = 0
    chunk_count = 0
    with h5py.File(nwb_file, "r") as h5f:
        maxwell_datasets: list[h5py.Dataset] = []

        def collect(_name: str, item) -> None:
            if isinstance(item, h5py.Dataset) and MAXWELL_FILTER_ID in _hdf5_filter_ids(item):
                maxwell_datasets.append(item)

        h5f.visititems(collect)
        for dataset in maxwell_datasets:
            if dataset.chunks is None:
                fail(f"Maxwell filter 401 dataset is not chunked: {dataset.name}")

            filter_ids = _hdf5_filter_ids(dataset)
            filter_index = filter_ids.index(MAXWELL_FILTER_ID)
            maxwell_mask_bit = 1 << filter_index
            observed_chunks = 0
            skipped_chunk = None

            def inspect_chunk(chunk_info, mask_bit=maxwell_mask_bit):
                nonlocal observed_chunks, skipped_chunk
                observed_chunks += 1
                if int(chunk_info.filter_mask) & mask_bit:
                    skipped_chunk = {
                        "offset": tuple(int(value) for value in chunk_info.chunk_offset),
                        "filter_mask": int(chunk_info.filter_mask),
                    }
                    return 1
                return None

            dataset.id.chunk_iter(inspect_chunk)
            if skipped_chunk is not None:
                fail(
                    "Maxwell filter 401 was skipped for an allocated HDF5 chunk, "
                    "which cannot be represented by one dataset-level LINDI filter: "
                    f"dataset={dataset.name}, chunk={json.dumps(skipped_chunk, sort_keys=True)}"
                )
            dataset_count += 1
            chunk_count += observed_chunks

    if dataset_count == 0:
        fail(f"No HDF5 dataset using Maxwell filter {MAXWELL_FILTER_ID} was found.")
    return dataset_count, chunk_count


def _patch_lindi_filter_mapping_for_maxwell() -> None:
    """Teach LINDI to declare Maxwell filter 401 as an explicit NumCodecs filter."""
    import importlib

    conversion_module = importlib.import_module("lindi.conversion.h5_filters_to_codecs")
    store_module = importlib.import_module("lindi.LindiH5ZarrStore.LindiH5ZarrStore")
    if getattr(store_module.InlineArray, "_braingeneers_native401", False):
        return

    original = conversion_module.h5_filters_to_codecs

    def patched(h5obj):
        try:
            return original(h5obj)
        except RuntimeError as exc:
            if "filter id 401" in str(exc):
                filter_ids = _hdf5_filter_ids(h5obj)
                if filter_ids != [MAXWELL_FILTER_ID]:
                    raise RuntimeError(
                        "Maxwell filter 401 is only supported as the sole HDF5 filter "
                        f"for a dataset; dataset={h5obj.name}, filters={filter_ids}"
                    ) from exc
                return [MaxwellMxwDataCodec()]
            raise

    conversion_module.h5_filters_to_codecs = patched
    store_module.h5_filters_to_codecs = patched

    class NativeMaxwellArray(store_module.InlineArray):
        _braingeneers_native401 = True

        def __init__(self, dataset):
            if isinstance(dataset, h5py.Dataset) and MAXWELL_FILTER_ID in _hdf5_filter_ids(dataset):
                # LINDI normally decodes numeric arrays below 1000 values into
                # one inline Blosc chunk. Preserve native filter-401 references
                # and actual chunks at every size, without reading their values.
                self._additional_zarr_attributes = {}
                self._is_inline = False
                self._zarray_bytes = self._chunk_fname = self._chunk_bytes = None
            else:
                super().__init__(dataset)

    store_module.InlineArray = NativeMaxwellArray
    original_templates = store_module.LindiReferenceFileSystemStore.use_templates_in_rfs

    def complete_templates(reference_file):
        original_templates(reference_file)
        # LINDI templates URLs only after five occurrences. Smaller native files
        # use direct URLs; retain the explicit map required by our reader contract.
        reference_file.setdefault("templates", {})

    store_module.LindiReferenceFileSystemStore.use_templates_in_rfs = staticmethod(complete_templates)


def _write_lindi(
    *,
    nwb_file: Path,
    output_file: Path,
    source_url: str,
    threshold: int | None,
    contiguous_max_chunk_size: int | None,
) -> None:
    opts = lindi.LindiH5ZarrStoreOpts(
        num_dataset_chunks_threshold=threshold,
        contiguous_dataset_max_chunk_size=contiguous_max_chunk_size,
    )

    with lindi.LindiH5pyFile.from_hdf5_file(
        str(nwb_file),
        zarr_store_opts=opts,
        url=source_url,
    ) as lindi_file:
        generation_metadata = {
            "sourceNwbFile": str(nwb_file),
            "sourceUrl": source_url,
            "numDatasetChunksThreshold": threshold,
            "contiguousDatasetMaxChunkSize": contiguous_max_chunk_size,
            "maxwellFilter401FallbackApplied": False,
            "maxwellFilter401Codec": codec_config(),
        }
        lindi_file.write_lindi_file(str(output_file), generation_metadata=generation_metadata)


def main() -> None:
    args = parse_args()

    nwb_file = Path(args.nwb_file)
    output_file = Path(args.output_file)

    if not nwb_file.exists():
        fail(f"Input NWB file does not exist: {nwb_file}")

    if nwb_file.suffix != ".nwb":
        fail(f"Input file must end with .nwb: {nwb_file}")

    threshold = parse_optional_int(
        args.num_dataset_chunks_threshold,
        option_name="num-dataset-chunks-threshold",
        allow_zero=True,
    )
    contiguous_max_chunk_size = parse_optional_int(
        args.contiguous_dataset_max_chunk_size,
        option_name="contiguous-dataset-max-chunk-size",
        allow_zero=False,
    )

    output_file.parent.mkdir(parents=True, exist_ok=True)

    dataset_count, chunk_count = validate_maxwell_filter_masks(nwb_file)
    register_maxwell_mxw_data_codec()
    _patch_lindi_filter_mapping_for_maxwell()
    _write_lindi(
        nwb_file=nwb_file,
        output_file=output_file,
        source_url=args.source_url,
        threshold=threshold,
        contiguous_max_chunk_size=contiguous_max_chunk_size,
    )

    print(
        "[generate_lindi_metadata] Wrote LINDI sidecar: "
        f"{output_file} (source_url={args.source_url}, "
        f"num_dataset_chunks_threshold={threshold}, "
        f"contiguous_dataset_max_chunk_size={contiguous_max_chunk_size}, "
        f"maxwell_filter401_codec={codec_config()}, "
        f"maxwell_filter401_datasets={dataset_count}, "
        f"validated_filter401_chunks={chunk_count})"
    )


if __name__ == "__main__":
    main()
