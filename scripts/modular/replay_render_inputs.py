#!/usr/bin/env python3
"""Replay unchanged plotting capsules with explicit test-only draws and receipts.

Run in the original task image. Original science and task folders must be mounted
read-only; --output is a fresh writable directory. This is validation tooling,
never a production plotting default or a scientific recomputation stage.
"""
import argparse
from collections import Counter
import hashlib
import inspect
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import sys
import traceback

import numpy as np

from check_stage_science import normalized


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def array(value):
    if np.ma.isMaskedArray(value):
        return {"data": normalized(np.asarray(value.data)), "mask": normalized(np.ma.getmaskarray(value))}
    return normalized(np.asarray(value))


def figure_inputs(figure):
    def path(value):
        return {"vertices": array(value.vertices),
                "codes": None if value.codes is None else array(value.codes)}

    def text(value):
        return {"text": value.get_text(), "position": value.get_position(),
                "transform": value.get_transform().get_matrix(),
                "rotation": value.get_rotation(), "font": str(value.get_fontproperties()),
                "color": value.get_color(), "visible": value.get_visible(),
                "horizontal_alignment": value.get_horizontalalignment(),
                "vertical_alignment": value.get_verticalalignment()}

    def patch(value):
        return {"class": type(value).__name__, "path": path(value.get_path()),
                "transform": value.get_transform().get_matrix(),
                "facecolor": value.get_facecolor(), "edgecolor": value.get_edgecolor(),
                "linewidth": value.get_linewidth(), "linestyle": value.get_linestyle(),
                "fill": value.get_fill(), "alpha": value.get_alpha(),
                "visible": value.get_visible(), "zorder": value.get_zorder()}

    axes = []
    for ax in figure.axes:
        items = {"title": ax.get_title(), "xlabel": ax.get_xlabel(), "ylabel": ax.get_ylabel(),
                 "xlim": ax.get_xlim(), "ylim": ax.get_ylim(), "lines": [], "images": [], "collections": [],
                 "patches": [patch(value) for value in ax.patches],
                 "background": patch(ax.patch), "texts": [text(value) for value in ax.texts]}
        legend = ax.get_legend()
        items["legend_texts"] = [] if legend is None else [text(value) for value in legend.get_texts()]
        assert not ax.tables, "Add explicit table data coverage before replaying a table artist"
        for line in ax.lines:
            items["lines"].append({"x": array(line.get_xdata()), "y": array(line.get_ydata()),
                                   "color": line.get_color(), "width": line.get_linewidth(),
                                   "alpha": line.get_alpha(), "style": line.get_linestyle(),
                                   "marker": str(line.get_marker()), "markersize": line.get_markersize(),
                                   "drawstyle": line.get_drawstyle(), "visible": line.get_visible()})
        for image in ax.images:
            items["images"].append({"data": array(image.get_array()), "clim": image.get_clim(),
                                    "extent": image.get_extent(), "cmap": image.get_cmap().name})
        for collection in ax.collections:
            item = {"class": type(collection).__name__, "offsets": array(collection.get_offsets()),
                    "array": None if collection.get_array() is None else array(collection.get_array()),
                    "clim": collection.get_clim(), "facecolors": array(collection.get_facecolors()),
                    "edgecolors": array(collection.get_edgecolors()), "alpha": collection.get_alpha(),
                    "paths": [path(value) for value in collection.get_paths()],
                    "linewidths": array(collection.get_linewidths()),
                    "transform": array(collection.get_transform().get_matrix()),
                    "offset_transform": array(collection.get_offset_transform().get_matrix())}
            if hasattr(collection, "get_sizes"):
                item["sizes"] = array(collection.get_sizes())
            if hasattr(collection, "get_coordinates"):
                item["coordinates"] = array(collection.get_coordinates())
            if hasattr(collection, "get_segments"):
                item["segments"] = [array(segment) for segment in collection.get_segments()]
            items["collections"].append(item)
        axes.append(items)
    return normalized({"size_inches": figure.get_size_inches(), "axes": axes,
                       "texts": [text(value) for value in figure.texts],
                       "artist_classes": dict(Counter(type(value).__module__ + "." + type(value).__name__
                                                      for value in figure.findobj()))})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("report_generation", "visualization"), required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--expected-report-choices", type=int, default=15,
                        help="Expected waveform draw count for this test control (default: 15)")
    parser.add_argument("--expected-report-status", default="ok",
                        choices=("ok", "no_detected_units", "no_curated_units"),
                        help="Expected report status for this test control (default: ok)")
    args = parser.parse_args()
    if args.expected_report_choices < 0:
        parser.error("--expected-report-choices must be nonnegative")
    task, out = args.task.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    capsule = out / "capsule"
    capsule.mkdir()
    shutil.copytree(task / "capsule/code", capsule / "code")
    (capsule / "data").symlink_to(task / "capsule/data")
    (capsule / "results").mkdir()
    (capsule / "scratch").mkdir()
    code = capsule / "code/run_capsule.py"
    receipt = {"role": args.role, "source_sha256": digest(code), "original_task": str(task),
               "validation_only_seed": args.seed, "choices": [], "random_chunks": [],
               "random_slices": [], "figures": [], "resolved_input_bases": [], "error": None,
               "original_unseeded_draws_observed": False,
               "original_products": {str(path.relative_to(task / "capsule/results")): {
                   "bytes": path.stat().st_size, "sha256": digest(path)}
                   for path in (task / "capsule/results").rglob("*") if path.is_file()}}
    np.random.seed(args.seed)
    import spikeinterface as si
    import spikeinterface.core.recording_tools as recording_tools
    from matplotlib.figure import Figure
    from threadpoolctl import threadpool_info

    original_choice = np.random.choice
    def choice(*values, **kwargs):
        result = original_choice(*values, **kwargs)
        frame = inspect.currentframe().f_back
        receipt["choices"].append({"caller": Path(frame.f_code.co_filename).name,
                                    "line": frame.f_lineno, "arguments": normalized(values),
                                    "kwargs": normalized(kwargs), "indices": normalized(result),
                                    "indices_values": np.asarray(result).tolist()})
        return result
    np.random.choice = choice
    original_slices = recording_tools.get_random_recording_slices
    def slices(*values, **kwargs):
        result = original_slices(*values, **kwargs)
        receipt["random_slices"].append({"kwargs": normalized(kwargs), "slices": normalized(result)})
        return result
    recording_tools.get_random_recording_slices = slices
    original_chunks = si.get_random_data_chunks
    def chunks(recording, *values, **kwargs):
        assert not values and "seed" not in kwargs, "Unexpected pre-existing render sampling control"
        frame = inspect.currentframe().f_back
        assert Path(frame.f_code.co_filename).name == "run_capsule.py"
        result = original_chunks(recording, seed=args.seed, **kwargs)
        receipt["random_chunks"].append({"line": frame.f_lineno, "seed": args.seed,
                                         "channels": normalized(recording.channel_ids),
                                         "frequency": recording.sampling_frequency,
                                         "chunk": array(result),
                                         "quantile_99_times_1p2": float(np.quantile(result, 0.99) * 1.2)})
        return result
    si.get_random_data_chunks = chunks
    original_load = si.load
    def load(*values, **kwargs):
        if kwargs.get("base_folder") is not None:
            # The data symlink names the original declared serialization base.
            # Resolve it explicitly rather than moving/rebasing the lazy graph.
            base = Path(kwargs["base_folder"]).resolve()
            receipt["resolved_input_bases"].append(str(base))
            kwargs["base_folder"] = base
        return original_load(*values, **kwargs)
    si.load = load
    original_save = Figure.savefig
    def save(figure, filename, *values, **kwargs):
        name = Path(filename).name if isinstance(filename, (str, Path)) else type(filename).__name__
        receipt["figures"].append({"product": name, "inputs": figure_inputs(figure),
                                    "save_options": normalized(kwargs)})
        return original_save(figure, filename, *values, **kwargs)
    Figure.savefig = save
    try:
        os.chdir(capsule / "code")
        if args.role == "report_generation":
            line = next(line for line in (task / ".command.sh").read_text().splitlines()
                        if line.startswith("python capsule/code/run_capsule.py"))
            argv = shlex.split(line)[2:]
            index = argv.index("--analyzer-dir") + 1
            argv[index] = str(task / argv[index])
            index = argv.index("--output-dir") + 1
            directory = capsule / "results" / Path(argv[index]).name
            directory.mkdir()
            argv[index] = str(directory)
        else:
            argv = []
        sys.argv = [str(code), *argv]
        receipt["capsule_argv"] = sys.argv[:]
        runpy.run_path(str(code), run_name="__main__")
        receipt["effective_job_kwargs"] = si.get_global_job_kwargs()
        receipt["thread_environment"] = {key: os.environ.get(key) for key in (
            "CO_CPUS", "N_JOBS_EXT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS")}
        receipt["threadpools"] = threadpool_info()
        assert receipt["figures"], "No figures were rendered"
        if args.role == "report_generation":
            receipt["expected_report_choices"] = args.expected_report_choices
            receipt["expected_report_status"] = args.expected_report_status
            receipt["report_summary"] = json.loads((directory / "report_summary.json").read_text())
            assert len(receipt["choices"]) == args.expected_report_choices and not receipt["random_chunks"]
            assert receipt["report_summary"]["status"] == args.expected_report_status
        else:
            assert receipt["random_chunks"] and receipt["random_slices"]
            assert len(list((capsule / "results").rglob("*.png"))) == 4
    except Exception:
        receipt["error"] = traceback.format_exc()
    receipt["passed"] = receipt["error"] is None
    receipt["products"] = {str(path.relative_to(capsule / "results")): {"bytes": path.stat().st_size,
                            "sha256": digest(path)} for path in (capsule / "results").rglob("*") if path.is_file()}
    (out / "render-inputs.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"passed": receipt["passed"], "role": args.role,
                      "figures": len(receipt["figures"]), "choices": len(receipt["choices"]),
                      "random_chunks": len(receipt["random_chunks"]), "error": receipt["error"]}, indent=2))
    raise SystemExit(0 if receipt["passed"] else 1)


if __name__ == "__main__":
    main()
