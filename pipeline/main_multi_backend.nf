#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

include { loadLaunchSettings; processOptions } from './settings'
include { job_dispatch } from './modules/job_dispatch'
include { preprocessing } from './modules/preprocessing'
include { spikesort_kilosort25 } from './modules/spikesort_kilosort25'
include { spikesort_kilosort4 } from './modules/spikesort_kilosort4'
include { spikesort_spykingcircus2 } from './modules/spikesort_spykingcircus2'
include { spikesort_lupin } from './modules/spikesort_lupin'
include { postprocessing } from './modules/postprocessing'
include { curation } from './modules/curation'
include { visualization } from './modules/visualization'
include { results_collector } from './modules/results_collector'
include { quality_control } from './modules/quality_control'
include { quality_control_collector } from './modules/quality_control_collector'
include { nwb_ecephys } from './modules/nwb_ecephys'
include { nwb_units } from './modules/nwb_units'
include { report_generation } from './modules/report_generation'
include { burst_detection } from './modules/burst_detection'

// Compatibility defaults belong to this direct caller, not imported processes.
params.ecephys_path = System.getenv('DATA_PATH') ?: System.getenv('DATA_DIR')
params.results_path = System.getenv('RESULTS_PATH') ?: "${launchDir}/results"
params.params_file = null
params.torch_device = null
// null preserves Nextflow's normal/resume publication defaults.
params.publish_overwrite = null
params.git_repo_prefix = System.getenv('GIT_REPO_PREFIX') ?: 'https://github.com/AllenNeuralDynamics/aind-'
params.report_container_image = 'braingeneers/mea-ephys-analysis:0.1.0'

launch_settings = loadLaunchSettings(params, baseDir)
sorter = launch_settings.sorter

workflow {
    // Input channel from ecephys path
    ecephys_ch = Channel.fromPath(params.ecephys_path, type: 'dir', checkIfExists: true)

    // Job dispatch
    job_dispatch_out = job_dispatch(
        Channel.value(processOptions('job_dispatch', launch_settings)),
        Channel.value(file("${baseDir}/../capsules/job_dispatch/code")),
        ecephys_ch.collect()
    )

    max_duration_file = job_dispatch_out.max_duration_file
    max_duration_minutes = max_duration_file.map { it.text.trim() }
    max_duration_minutes.view { "Max recording duration: ${it}min" }

    // Preprocessing
    preprocessing_out = preprocessing(
        Channel.value(processOptions('preprocessing', launch_settings)),
        Channel.value(file("${baseDir}/../capsules/preprocessing/code")),
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.flatten()
    )

    // Spike sorting based on selected sorter
    if (sorter == 'kilosort25') {
        spikesort_out = spikesort_kilosort25(
            Channel.value(processOptions('spikesort_kilosort25', launch_settings)),
            max_duration_minutes,
            preprocessing_out.results
        )
    } else if (sorter == 'kilosort4') {
        spikesort_out = spikesort_kilosort4(
            Channel.value(processOptions('spikesort_kilosort4', launch_settings)),
            Channel.value(file("${baseDir}/../capsules/kilosort4/code")),
            max_duration_minutes,
            preprocessing_out.results
        )
    } else if (sorter == 'spykingcircus2') {
        spikesort_out = spikesort_spykingcircus2(
            Channel.value(processOptions('spikesort_spykingcircus2', launch_settings)),
            max_duration_minutes,
            preprocessing_out.results
        )
    } else if (sorter == 'lupin') {
        spikesort_out = spikesort_lupin(
            Channel.value(processOptions('spikesort_lupin', launch_settings)),
            max_duration_minutes,
            preprocessing_out.results
        )
    } else {
        error "Unsupported sorter: ${sorter}"
    }

    // Postprocessing
    postprocessing_out = postprocessing(
        Channel.value(processOptions('postprocessing', launch_settings)),
        Channel.value(file("${baseDir}/../capsules/postprocessing/code")),
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.flatten(),
        preprocessing_out.results.collect(),
        spikesort_out.results.collect()
    )

    // Curation
    curation_out = curation(
        Channel.value(processOptions('curation', launch_settings)),
        max_duration_minutes,
        postprocessing_out.results
    )

    // Visualization
    visualization_out = visualization(
        Channel.value(processOptions('visualization', launch_settings)),
        Channel.value(file("${baseDir}/../capsules/visualization/code")),
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.collect(),
        preprocessing_out.results,
        spikesort_out.results.collect(),
        postprocessing_out.results.collect(),
        curation_out.results.collect()
    )

    // Results collection
    results_collector_out = results_collector(
        Channel.value(processOptions('results_collector', launch_settings)),
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.collect(),
        preprocessing_out.results.collect(),
        spikesort_out.results.collect(),
        postprocessing_out.results.collect(),
        curation_out.results.collect(),
        visualization_out.results.collect()
    )
    // Stage the code in this checkout and select one analyzer explicitly per recording.
    analyzer_ch = postprocessing_out.results.flatten()
        .filter { it.name.startsWith('postprocessed_') && it.name.endsWith('.zarr') }
        .map { analyzer -> tuple(analyzer.name.replaceFirst(/^postprocessed_/, '').replaceFirst(/\.zarr$/, ''), analyzer) }
    report_generation_out = report_generation(
        Channel.value(processOptions('report_generation', launch_settings)),
        analyzer_ch,
        Channel.value(file("${baseDir}/../capsules/report_generation"))
    )
    burst_detection(
        Channel.value(processOptions('burst_detection', launch_settings)),
        report_generation_out.results.map { id, folder -> tuple(id, folder.resolve('spike_times.npy')) },
        Channel.value(file("${baseDir}/../capsules/burst_detection"))
    )
    // QC modules remain available but are not invoked, matching the baseline.

    // NWB ecephys
    nwb_ecephys_out = nwb_ecephys(
        Channel.value(processOptions('nwb_ecephys', launch_settings)),
        Channel.value(file("${baseDir}/../capsules/nwb_ecephys/code")),
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.collect(),
    )

    // NWB units
    nwb_units(
        Channel.value(processOptions('nwb_units', launch_settings)),
        Channel.value(file("${baseDir}/../capsules/nwb_units/code")),
        max_duration_minutes,
        ecephys_ch.collect(),
        job_dispatch_out.results.collect(),
        results_collector_out.nwb_data.collect(),
        nwb_ecephys_out.results.collect()
    )
}
