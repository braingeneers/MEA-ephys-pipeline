import warnings

warnings.filterwarnings("ignore")

import os
import sys
import argparse
from pathlib import Path
import shutil
import json
import time
from datetime import datetime, timezone, UTC
from packaging.version import parse
import numpy as np
import pandas as pd
import logging
import zarr
import numcodecs

# SpikeInterface
import spikeinterface as si
from spikeinterface.core.core_tools import extractor_dict_iterator, set_value_in_extractor_dict

# AIND
from aind_data_schema_models.modalities import Modality
from aind_data_schema_models.organizations import Organization
from aind_data_schema_models.data_name_patterns import DataLevel, build_data_name

from aind_data_schema.components.identifiers import Person
from aind_data_schema.components.identifiers import Code
from aind_data_schema.core.data_description import Funding, DataDescription
from aind_data_schema.core.processing import (
    DataProcess,
    Processing,
    ProcessName,
    ProcessStage,
    ResourceTimestamped,
    ResourceUsage,
)

from aind_metadata_upgrader.data_description.v1v2 import DataDescriptionV1V2
from aind_metadata_upgrader.processing.v1v2 import ProcessingV1V2

try:
    from aind_log_utils import log

    HAVE_AIND_LOG_UTILS = True
except ImportError:
    HAVE_AIND_LOG_UTILS = False

PIPELINE_MAINAINER = "Alessio Buccino"
PIPELINE_NAME = "AIND Ephys Pipeline"
PIPELINE_URL = os.getenv("PIPELINE_URL", "")
PIPELINE_VERSION = os.getenv("PIPELINE_VERSION", "")
ADS_VERSION = "2.4.0"


data_folder = Path("../data/")
results_folder = Path("../results/")


# Create an argument parser
parser = argparse.ArgumentParser(description="Collect results from Ephys pipeline")

process_name_group = parser.add_mutually_exclusive_group()
process_name_help = "Process name to use in the derived data description."
process_name_group.add_argument(
    "--process-name", default="sorted", help=process_name_help
)
process_name_group.add_argument("static_process_name", nargs="?", help=process_name_help)

parser.add_argument(
    "--pipeline-data-path",
    default=None,
    help="Path to the data folder containing the ecephys session.",
)

parser.add_argument(
    "--pipeline-results-path",
    default=None,
    help="Path to the results folder where the collected results will be saved.",
)


def remap_extractor_path(recording_dict, base_folder, relative_to=None):
    """
    This function remaps the file_path and folder_path in the recording_dict
    to be absolute or relative paths, resolving any symlinks if they exist.
    """
    # Use the same physical coordinates for targets and their serialization base.
    # A future publication directory need not exist yet.
    base_folder = Path(base_folder).resolve()
    if relative_to is not None:
        relative_to = Path(relative_to).resolve()
    path_list_iter = extractor_dict_iterator(recording_dict)
    access_paths = {}
    for path_iter in path_list_iter:
        if path_iter.name in ("file_path", "folder_path"):
            access_path = path_iter.access_path
            access_paths[access_path] = path_iter.value
    # make paths absolute
    if relative_to is None:
        recording_dict["relative_paths"] = False
    for access_path, path in access_paths.items():
        # check if the absolute path is a symlink
        absolute_path = base_folder / path

        if absolute_path.exists():
            logging.info(f"\tResolving path for {access_path[-1]} - {path}")
            absolute_path = absolute_path.resolve()
            if relative_to is not None:
                new_path = os.path.relpath(absolute_path, relative_to)
            else:
                new_path = absolute_path
            set_value_in_extractor_dict(recording_dict, access_path, str(new_path))
    return recording_dict


def fix_process_names(process_dicts: list):
    """
    This function ensures that process names are unique.
    """
    process_names = [d["name"] for d in process_dicts]

    if len(process_names) != len(set(process_names)):
        logging.info(f"Non-unique process names!")
        unique_names, counts = np.unique(process_names, return_counts=True)
        duplicated_names = list(unique_names[np.nonzero(counts > 1)])
        logging.info(f"\tDuplicated names: {duplicated_names}")
        offset = 1
        new_process_dicts = []
        for process in process_dicts:
            if process["name"] in duplicated_names:
                existing_name = process["name"]
                new_name = f"{existing_name}_{offset}"
                process["name"] = new_name
                logging.info(f"\tUpdated {existing_name} to {new_name}")
                offset += 1
            new_process_dicts.append(process)
        return new_process_dicts
    else:
        return process_dicts



if __name__ == "__main__":
    ###### COLLECT RESULTS #########
    t_collection_start = time.perf_counter()
    args = parser.parse_args()
    process_name = args.static_process_name or args.process_name
    pipeline_data_path = args.pipeline_data_path
    pipeline_results_path = args.pipeline_results_path

    # check if test
    if (data_folder / "postprocessing_pipeline_output_test").is_dir():
        logging.info("\n*******************\n**** TEST MODE ****\n*******************\n")
        postprocessed_folder = data_folder / "postprocessing_pipeline_output_test"
        preprocessed_folder = data_folder / "preprocessing_pipeline_output_test"
        spikesorted_folder = data_folder / "spikesorting_pipeline_output_test"
        curated_folder = data_folder / "curation_pipeline_output_test"
        unit_classifier_folder = data_folder / "unit_classifier_pipeline_output_test"
        visualization_folder = data_folder / "visualization_pipeline_output_test"

        test_folders = [
            preprocessed_folder,
            spikesorted_folder,
            postprocessed_folder,
            curated_folder,
            visualization_folder,
        ]

        data_process_files = []
        for test_folder_name in test_folders:
            test_folder = data_folder / test_folder_name
            data_process_files.extend(
                [p for p in test_folder.iterdir() if "data_process" in p.name and p.name.endswith(".json")]
            )
    else:
        postprocessed_folder = data_folder
        preprocessed_folder = data_folder
        spikesorted_folder = data_folder
        curated_folder = data_folder
        unit_classifier_folder = data_folder
        visualization_folder = data_folder
        data_process_files = [
            p for p in data_folder.iterdir() if "data_process" in p.name and p.name.endswith(".json")
        ]

    ecephys_sessions = [p for p in data_folder.iterdir() if "ecephys" in p.name.lower()]
    assert len(ecephys_sessions) == 1, f"Attach one session at a time {ecephys_sessions}"
    ecephys_session_folder = ecephys_sessions[0]

    if HAVE_AIND_LOG_UTILS:
        # look for subject.json and data_description.json files
        subject_json = ecephys_session_folder / "subject.json"
        subject_id = "undefined"
        if subject_json.is_file():
            subject_data = json.load(open(subject_json, "r"))
            subject_id = subject_data["subject_id"]

        data_description_json = ecephys_session_folder / "data_description.json"
        session_name = "undefined"
        if data_description_json.is_file():
            data_description = json.load(open(data_description_json, "r"))
            session_name = data_description["name"]

        log.setup_logging(
            "Collect Results Ecephys",
            subject_id=subject_id,
            asset_name=session_name,
        )
    else:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")

    logging.info("\n\nCOLLECTING RESULTS")

    json_files = [p for p in data_folder.iterdir() if "job" in p.name and p.suffix == ".json"]
    # load session_name from any JSON file
    if len(json_files) > 0:
        with open(json_files[0], "r") as f:
            job_dict = json.load(f)
        session_name = job_dict["session_name"]
        logging.info(f"Loaded session name from JSON files: {session_name}")
    else:
        session_name = ecephys_session_folder.name

    # Move spikesorted / postprocessing / curated
    spikesorted_results_folder = results_folder / "spikesorted"
    spikesorted_results_folder.mkdir(exist_ok=True)
    preprocessed_results_folder = results_folder / "preprocessed"
    preprocessed_results_folder.mkdir(exist_ok=True)
    postprocessed_results_folder = results_folder / "postprocessed"
    postprocessed_results_folder.mkdir(exist_ok=True)
    curated_results_folder = results_folder / "curated"
    curated_results_folder.mkdir(exist_ok=True)

    # PREPROCESSED
    logging.info("Copying preprocessed folders to results:")
    preprocessed_json_files = [
        p for p in preprocessed_folder.iterdir() if "preprocessed_" in p.name and p.name.endswith(".json")
    ]
    (results_folder / "preprocessed").mkdir(exist_ok=True)
    for preprocessed_file in preprocessed_json_files:
        recording_json_file_name = preprocessed_file.name[len("preprocessed_") :]
        recording_name = preprocessed_file.stem[len("preprocessed_") :]
        recording_output_json_file = preprocessed_results_folder / recording_json_file_name
        logging.info(f"\t{recording_name}")

        with open(preprocessed_file, "r") as f:
            recording_dict = json.load(f)

        # running locally or on HPC, we need to resolve symlinks in the recording_dict
        if pipeline_data_path is not None:
            logging.info(f"\tRemapping preprocessed recording JSON paths relative to {pipeline_data_path}")
            recording_dict = remap_extractor_path(
                recording_dict=recording_dict,
                base_folder=data_folder,
                relative_to=pipeline_data_path
            )
        recording_dict_str = json.dumps(recording_dict, indent=4)
        if pipeline_data_path is None and "ecephys_session" in recording_dict_str:
            logging.info(f"\tRemapping preprocessed recording: 'ecephys_session' -> '{session_name}'")
            recording_dict_str = recording_dict_str.replace("ecephys_session", session_name)
        recording_output_json_file.write_text(recording_dict_str, encoding="utf8")

    # MOTION
    motion_folders = [
        p for p in preprocessed_folder.iterdir() if "motion_" in p.name and p.is_dir()
    ]
    if len(motion_folders) > 0:
        logging.info("Copying motion folders to results:")
        motion_results_folder = preprocessed_results_folder / "motion"
        motion_results_folder.mkdir(exist_ok=True)
        for motion_folder in motion_folders:
            recording_name = motion_folder.name[len("motion_") :]
            logging.info(f"\t{recording_name}")
            shutil.copytree(motion_folder, motion_results_folder / recording_name)

    # SPIKESORTED
    logging.info("Copying spikesorted folders to results:")
    spikesorted_folders = [p for p in spikesorted_folder.iterdir() if "spikesorted_" in p.name and p.is_dir()]
    for f in spikesorted_folders:
        recording_name = f.name[len("spikesorted_") :]
        logging.info(f"\t{recording_name}")
        shutil.copytree(f, spikesorted_results_folder / recording_name)

    # POSTPROCESSED / CURATED
    logging.info("Copying postprocessed and curated folders to results:")
    postprocessed_folders = [
        p
        for p in postprocessed_folder.iterdir()
        if "postprocessed" in p.name and p.is_dir()
    ]
    for postprocessed_input_folder in postprocessed_folders:
        recording_name = postprocessed_input_folder.stem[len("postprocessed_") :]
        recording_folder_name = f"{recording_name}.zarr"
        analyzer_output_folder = None
        logging.info(f"\t{recording_name}")
        try:
            # we first check if the input postprocessed folder is valid
            # this will raise an Exception if it fails, preventing to copy
            # to results
            analyzer = si.load(postprocessed_input_folder, load_extensions=False)
            analyzer_output_folder = postprocessed_results_folder / recording_folder_name
            shutil.copytree(postprocessed_input_folder, analyzer_output_folder)
            # we reload the analyzer to results to be able to append properties
            analyzer = si.load(analyzer_output_folder, load_extensions=False)
        except:
            logging.info(f"\t\tSpike sorting failed on {recording_name}. Skipping collection")
            continue

        # add defaut_qc property
        default_qc = None
        decoder_label = None
        curation_file = curated_folder / f"qc_{recording_name}.npy"
        if curation_file.is_file():
            default_qc = np.load(curation_file)
            if len(default_qc) == len(analyzer.unit_ids):
                analyzer.set_sorting_property("default_qc", default_qc, save=True)
        # add classifier
        unit_classifier_file = unit_classifier_folder / f"unit_classifier_{recording_name}.csv"
        if unit_classifier_file.is_file():
            unit_classifier_df = pd.read_csv(unit_classifier_file, index_col=False)
            if len(unit_classifier_df) == len(analyzer.unit_ids):
                decoder_label = np.array(unit_classifier_df["decoder_label"].values).astype("str")
                analyzer.set_sorting_property("decoder_label", decoder_label, save=True)
                decoder_probability = np.array(unit_classifier_df["decoder_probability"].values).astype(float)
                analyzer.set_sorting_property("decoder_probability", decoder_probability, save=True)

        _ = analyzer.sorting.save(folder=curated_results_folder / recording_name)

        # If the collect results runs in a pipeline, we need to further modify the mappings of the preprocessed recording in the analyzer.
        # For the postprocessed capsule, the analyzer is in:
        # "root/results/postprocessed_{recording_name}.zarr", so data folder is "../../data"
        # After a pipeline run, two additional subfolders are added, and the sorted asset will be mounted as:
        # "root/data/{sorted_session_name}/postprocessed/{recording_name}.zarr"
        # we therefore need to replace "../../" with "../../../.." in order to have the anlyzer automatically find and reload the preprocessed recording
        AWS_BATCH_EXECUTOR = os.getenv("AWS_BATCH_JOB_ID") is not None

        analyzer_root = zarr.open(analyzer_output_folder, mode="r+")
        recording_root = analyzer_root["recording"]
        object_codec = None
        if isinstance(recording_root.filters[0], numcodecs.JSON):
            object_codec = numcodecs.JSON()
        elif isinstance(recording_root.filters[0], numcodecs.Pickle):
            object_codec = numcodecs.Pickle()
        if object_codec is not None:
            recording_dict = recording_root[0]
            if pipeline_results_path is not None:
                # here we need to resolve the recording path, make it relative to the pipeline results path
                pipeline_postprocessed_output = Path(pipeline_results_path) / "postprocessed" / recording_folder_name
            elif AWS_BATCH_EXECUTOR:
                # here we need to add a new subfolder for the session name
                pipeline_postprocessed_output = results_folder / "postprocessed" / session_name / recording_folder_name
            else:
                # here we just add the postprocessed folder to the results folder
                pipeline_postprocessed_output = results_folder / "postprocessed" / recording_folder_name
            logging.info(f"\t\tRemapping recording path for postprocessed to {pipeline_postprocessed_output}")
            recording_dict_mapped = remap_extractor_path(
                recording_dict=recording_dict,
                base_folder=postprocessed_input_folder,
                relative_to=pipeline_postprocessed_output
            )
            # update the "ecephys_session" field in the recording_dict, if present
            recording_dict_str = json.dumps(recording_dict_mapped, indent=4)
            if pipeline_data_path is None:
                recording_dict_str = recording_dict_str.replace("ecephys_session", session_name)
            recording_dict_mapped = json.loads(recording_dict_str)
            # remove the old recording and add the new one
            del analyzer_root["recording"]
            zarr_rec = np.array([recording_dict_mapped], dtype=object)
            analyzer_root.create_dataset("recording", data=zarr_rec, object_codec=object_codec)
            zarr.consolidate_metadata(analyzer_root.store)
        else:
            logging.info(f"Unsupported recording object codec: {recording_root.filters[0]}. Cannot remap recording path")

    # VISUALIZATION
    logging.info("Copying visualization outputs to results:")
    visualization_output = {}
    visualization_json_files = [
        p
        for p in visualization_folder.iterdir()
        if "visualization_" in p.name and p.name.endswith(".json") and "data_process" not in p.name
    ]
    for visualization_json_file in visualization_json_files:
        recording_name = visualization_json_file.name[len("visualization_") : len(visualization_json_file.name) - 5]
        logging.info(f"\t{recording_name}")
        with open(visualization_json_file, "r") as f:
            visualization_dict = json.load(f)
        visualization_output[recording_name] = visualization_dict
    with open(results_folder / "visualization_output.json", "w") as f:
        json.dump(visualization_output, f, indent=4)

    # Visualization folders
    visualization_folders = [
        p for p in visualization_folder.iterdir() if p.is_dir() and p.name.startswith("visualization_")
    ]
    if len(visualization_folders) > 0:
        visualization_output_folder = results_folder / "visualization"
        visualization_output_folder.mkdir(exist_ok=True)
        for viz_folder in visualization_folders:
            recording_name = viz_folder.name[len("visualization_") :]
            shutil.copytree(viz_folder, visualization_output_folder / recording_name)

    # PROCESSING
    logging.info("Generating processing metadata")
    processing_upgrader = ProcessingV1V2()
    ephys_data_processes = []
    for json_file in data_process_files:
        stream_name = "_".join(json_file.stem.split("_")[3:])
        with open(json_file, "r") as data_process_file:
            data_process_data = json.load(data_process_file)

            if "code" not in data_process_data:
                # The 'code' field was added in 2.0. This is mainly used for testing with previously generated 1.0 data processes
                data_process_data = processing_upgrader._convert_v1_process_to_v2(data_process_data, stage="Processing")

            # Append stream name to make data processes unique
            data_process_name = data_process_data["name"]
            data_process_data["name"] = f"{data_process_name} - {stream_name}"
            data_process_data["pipeline_name"] = PIPELINE_NAME
            ephys_data_processes.append(data_process_data)

    existing_data_processes = []
    existing_pipelines = []
    if (ecephys_session_folder / "processing.json").is_file():
        with open(ecephys_session_folder / "processing.json", "r") as processing_file:
            processing_data = json.load(processing_file)
        if parse(processing_data["schema_version"]) < parse("2.0.0"):
            logging.warning(
                "DEPRECATION: processing.json is aind-data-schema 1.0 and will stop being supported in April 2026. "
                "Please upgrade to aind-data-schema v2.0."
            )
            try:
                upgraded_processing_data = processing_upgrader.upgrade(processing_data, schema_version=ADS_VERSION)
                existing_data_processes = upgraded_processing_data.get("data_processes", [])
                logging.info(f"Upgraded data processes to {ADS_VERSION}.")
            except Exception as e:
                logging.info(f"Failed upgrading processing for error: {e}\nCreating from scratch.")
        else:
            # validate existing data_processes
            failed_data_processes = []
            for data_process in processing_data["data_processes"]:
                try:
                    DataProcess.model_validate(data_process)
                    existing_data_processes.append(data_process)
                except Exception as e:
                    failed_data_processes.append(data_process)
            if len(failed_data_processes) > 0:
                failed_process_names = [d["name"] for d  in failed_data_processes]
                logging.info(f"Failed to validate existing data processes: {failed_process_names}")
            existing_pipelines = processing_data["pipelines"]


    all_data_process_dicts = existing_data_processes + ephys_data_processes
    # Ensure process names are unique
    all_data_process_dicts = fix_process_names(all_data_process_dicts)
    all_data_processes = [DataProcess(**d) for d in all_data_process_dicts]

    ephys_pipeline = Code(
        name=PIPELINE_NAME,
        url=PIPELINE_URL,
        version=PIPELINE_VERSION,
    )
    pipelines = existing_pipelines + [ephys_pipeline]
    processing = Processing.create_with_sequential_process_graph(
        pipelines=pipelines,
        data_processes=all_data_processes
    )
    processing.write_standard_file(output_directory=results_folder)

    # DATA_DESCRIPTION
    logging.info("Generating data_description metadata")
    data_description_data = None
    if (ecephys_session_folder / "data_description.json").is_file():
        with open(ecephys_session_folder / "data_description.json", "r") as data_description_file:
            data_description_data = json.load(data_description_file)

    if (ecephys_session_folder / "subject.json").is_file():
        with open(ecephys_session_folder / "subject.json", "r") as subject_file:
            subject_info = json.load(subject_file)
        subject_id = subject_info["subject_id"]
    else:
        subject_id = "000000"  # unknown

    data_description = None
    if data_description_data is not None:
        if parse(data_description_data["schema_version"]) < parse("2.0.0"):
            logging.warning(
                "DEPRECATION: data_description.json is aind-data-schema 1.0 and will stop being supported in April 2026. "
                "Please upgrade to aind-data-schema v2.0."
            )
            try:
                upgrader = DataDescriptionV1V2()
                # at least one investigator is required
                if len(data_description_data.get("investigators", [])) == 0:
                    data_description_data.update(dict(investigators=[dict(name="unkwnown")]))
                if len(data_description_data.get("funding_source", [])) == 0:
                    data_description_data.update(
                        dict(funding_source=[dict(funder="AIND")])
                    )
                upgraded_data_description_data = upgrader.upgrade(data_description_data, schema_version=ADS_VERSION)
                DataDescription.model_validate(upgraded_data_description_data)
                logging.info(f"Upgraded data processes to {ADS_VERSION}.")
                data_description = DataDescription(**upgraded_data_description_data)
                derived_data_description = DataDescription.from_raw(
                    data_description, process_name=process_name
                )
            except Exception as e:
                logging.info(f"Failed upgrading data description for error: {e}\nCreating from scratch.")
        else:
            try:
                DataDescription.model_validate(data_description_data)
                data_description = DataDescription(**data_description_data)
                derived_data_description = DataDescription.from_raw(
                    data_description, process_name=process_name
                )
            except Exception as e:
                logging.info(f"Failed to instantiate data description: {e}\nCreating from scratch.")

    if data_description is None:
        # make from scratch:
        now = datetime.now(UTC)
        derived_data_description = DataDescription(
            name=build_data_name(subject_id, creation_datetime=now),
            modalities=[Modality.ECEPHYS],
            subject_id=subject_id,
            creation_time=now,
            institution=Organization.AIND,
            investigators=[Person(name="unkwnown")],
            funding_source=[Funding(funder=Organization.AI)],
            project_name="unknown",
            data_level=DataLevel.DERIVED,
        )

    # save processing files to output
    derived_data_description.write_standard_file(output_directory=results_folder)

    # OTHER METADATA FILES
    logging.info("Propagating other metadata files")
    metadata_json_files = [
        p
        for p in ecephys_session_folder.iterdir()
        if p.suffix == ".json" and "processing" not in p.name
        and "data_description" not in p.name
        and "job" not in p.name
        and "metadata.nd" not in p.name
    ]
    for json_file in metadata_json_files:
        shutil.copy(json_file, results_folder)

    t_collection_end = time.perf_counter()
    elapsed_time_collection = np.round(t_collection_end - t_collection_start, 2)
    logging.info(f"COLLECTION time: {elapsed_time_collection}s")
