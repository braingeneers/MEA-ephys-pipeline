nextflow.enable.dsl = 2

include { meaCodeDigest } from '../code_identity'

process results_collector {
    tag 'result-collector'
    container { task_settings.container }
    // Merge payloads into the output port holding its atomic origin record.
    stageOutMode 'copy'

    // Keep option booleans static for Nextflow 23; saveAs selects one policy.
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', saveAs: { filename -> task_settings.publish_overwrite == null ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: true, saveAs: { filename -> task_settings.publish_overwrite == true ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: false, saveAs: { filename -> task_settings.publish_overwrite == false ? new File(filename).getName() : null }

    input:
    val task_settings
    tuple val(local_code_identity), path(local_code, stageAs: 'capsule-source')
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'
    path preprocessing_results, stageAs: 'capsule/data/*'
    path spikesort_results, stageAs: 'capsule/data/*'
    path postprocessing_results, stageAs: 'capsule/data/*'
    path curation_results, stageAs: 'capsule/data/*'
    path visualization_results, stageAs: 'capsule/data/*'

    output:
    tuple val(task_settings), val(execution_record), path('unit-results'), path('capsule/data'), emit: unit_results
    path 'capsule/results/*', emit: results
    path 'capsule/results/*', emit: nwb_data
    path 'capsule/results/*', emit: qc_data

    script:
    def staged_code_identity = meaCodeDigest([local_code])
    execution_record = [container: task.container ?: '', cpus: task.cpus,
        memory: task.memory?.toString(), time: task.time?.toString(), disk: task.disk?.toString(),
        accelerator: task.accelerator ? [request: task.accelerator.request, limit: task.accelerator.limit] : null]
    """
    #!/usr/bin/env bash
    # MEA staged-code SHA-256: ${staged_code_identity}
    set -e
    MEA_PRODUCER_TASK_ROOT=\$(pwd -P)
    ln -s capsule/results unit-results

    mkdir -p capsule
    mkdir -p capsule/data
    mkdir -p capsule/results
    mkdir -p capsule/scratch

    if [[ ${task_settings.executor_mode} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
    fi

    echo "[${task.tag}] staging local capsule code..."
    cp -rL capsule-source capsule/code
    # Vendored at the original pin; see capsules/results_collector/UPSTREAM.md.

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run --pipeline-data-path ${task_settings.input_path} --pipeline-results-path ${task_settings.results_path}

    cd "\$MEA_PRODUCER_TASK_ROOT"
    # MEA producer-location recorder SHA-256: ${task_settings.producer_location_identity}
    export MEA_PRODUCER_LOCATION_SHA="${task_settings.producer_location_identity}"
    ${task_settings.producer_location_script}

    echo "[${task.tag}] completed!"
    """
}
