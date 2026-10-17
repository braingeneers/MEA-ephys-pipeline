nextflow.enable.dsl = 2

process burst_detection {
    tag { "burst-detection:${recording_id}" }
    container { task_settings.container }
    // Keep option booleans static for Nextflow 23; saveAs selects one policy.
    publishDir { "${task_settings.results_path}/bursts" }, mode: 'copy', saveAs: { filename -> task_settings.publish_overwrite == null ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}/bursts" }, mode: 'copy', overwrite: true, saveAs: { filename -> task_settings.publish_overwrite == true ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}/bursts" }, mode: 'copy', overwrite: false, saveAs: { filename -> task_settings.publish_overwrite == false ? new File(filename).getName() : null }

    input:
    val task_settings
    tuple val(recording_id), path(spike_times, stageAs: 'capsule/data/spike_times.npy')
    path burst_code, stageAs: 'capsule/code'

    output:
    tuple val(recording_id), path('capsule/results/*'), emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e
    mkdir -p 'capsule/results/${recording_id}'
    python capsule/code/run_capsule.py \
        --spike-times capsule/data/spike_times.npy \
        --output-dir 'capsule/results/${recording_id}' \
        --plot-mode separate
    """
}
