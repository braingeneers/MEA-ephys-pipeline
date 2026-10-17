nextflow.enable.dsl = 2

process nwb_ecephys {
    tag 'nwb-ecephys'
    container { task_settings.container }

    input:
    val task_settings
    path local_code, stageAs: 'capsule-source'
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'

    output:
    path 'capsule/results/*', emit: results

    script:
    """
    #!/usr/bin/env bash
    set -e

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
    # Vendored at the original pin; see capsules/nwb_ecephys/UPSTREAM.md.

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    bash run ${task_settings.args}

    echo "[${task.tag}] completed!"
    """
}
