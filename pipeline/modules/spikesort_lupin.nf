nextflow.enable.dsl = 2

process spikesort_lupin {
    tag 'spikesort-lupin'
    container { task_settings.container }

    input:
    val task_settings
    val max_duration_minutes
    path preprocessing_results, stageAs: 'capsule/data/*'

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

    echo "[${task.tag}] cloning git repo..."
    ${task_settings.clone_script}
    clone_repo "${task_settings.git_repo_prefix}ephys-spikesort-lupin.git" "${task_settings.capsule_pin}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${task_settings.args} ${task_settings.job_args}

    echo "[${task.tag}] completed!"
    """
}
