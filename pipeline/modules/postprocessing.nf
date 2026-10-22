nextflow.enable.dsl = 2

include { meaCodeDigest } from '../code_identity'

process postprocessing {
    tag 'postprocessing'
    container { task_settings.container }
    // Merge payloads into the output port holding its atomic origin record.
    stageOutMode 'copy'

    input:
    val task_settings
    tuple val(local_code_identity), path(local_code, stageAs: 'capsule-source')
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'
    path preprocessing_results, stageAs: 'capsule/data/*'
    path spikesort_results, stageAs: 'capsule/data/*'

    output:
    tuple val(task_settings), val(execution_record), path('unit-results'), path('capsule/data'), emit: unit_results
    path 'capsule/results/*', emit: results

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
        # Make sure N_JOBS matches allocated CPUs on SLURM
        export N_JOBS_EXT=${task.cpus}
    fi

    echo "[${task.tag}] staging local capsule code..."
    cp -rL capsule-source capsule/code
    # Vendored at the original pin; see capsules/postprocessing/UPSTREAM.md.

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${task_settings.args} ${task_settings.job_args}

    cd "\$MEA_PRODUCER_TASK_ROOT"
    # MEA producer-location recorder SHA-256: ${task_settings.producer_location_identity}
    export MEA_PRODUCER_LOCATION_SHA="${task_settings.producer_location_identity}"
    ${task_settings.producer_location_script}

    echo "[${task.tag}] completed!"
    """
}
