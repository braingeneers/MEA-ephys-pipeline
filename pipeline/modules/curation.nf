nextflow.enable.dsl = 2

process curation {
    tag 'curation'
    container { task_settings.container }

    input:
    val task_settings
    val max_duration_minutes
    path postprocessing_results, stageAs: 'capsule/data/*'

    output:
    tuple val(task_settings), val(execution_record), path('unit-results'), path('capsule/data'), emit: unit_results
    path 'capsule/results/*', emit: results

    script:
    execution_record = [container: task.container ?: '', cpus: task.cpus,
        memory: task.memory?.toString(), time: task.time?.toString(), disk: task.disk?.toString(),
        accelerator: task.accelerator ? [request: task.accelerator.request, limit: task.accelerator.limit] : null]
    """
    #!/usr/bin/env bash
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

    echo "[${task.tag}] cloning git repo..."
    ${task_settings.clone_script}
    clone_repo "https://github.com/Varda006/aind-ephys-curation.git" "${task_settings.capsule_pin}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run ${task_settings.args} ${task_settings.job_args}

    echo "[${task.tag}] completed!"
    """
}
