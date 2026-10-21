nextflow.enable.dsl = 2

include { meaCodeDigest } from '../code_identity'

process nwb_ecephys {
    tag 'nwb-ecephys'
    container { task_settings.container }

    input:
    val task_settings
    tuple val(local_code_identity), path(local_code, stageAs: 'capsule-source')
    tuple val(source_metadata_helper_identity), path(source_metadata_helper, stageAs: 'source_nwb.py')
    tuple val(source_graph_helper_identity), path(source_graph_helper, stageAs: 'nwb_graph.py')
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'

    output:
    tuple val(task_settings), val(execution_record), path('unit-results'), path('capsule/data'), emit: unit_results
    path 'capsule/results/*', emit: results

    script:
    def staged_code_identity = meaCodeDigest([local_code, source_metadata_helper, source_graph_helper])
    execution_record = [container: task.container ?: '', cpus: task.cpus,
        memory: task.memory?.toString(), time: task.time?.toString(), disk: task.disk?.toString(),
        accelerator: task.accelerator ? [request: task.accelerator.request, limit: task.accelerator.limit] : null]
    """
    #!/usr/bin/env bash
    # MEA staged-code SHA-256: ${staged_code_identity}
    set -e
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
    cp source_nwb.py capsule/code/source_nwb.py
    cp nwb_graph.py capsule/code/nwb_graph.py
    # Vendored at the original pin; see capsules/nwb_ecephys/UPSTREAM.md.

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    bash run ${task_settings.args}

    echo "[${task.tag}] completed!"
    """
}
