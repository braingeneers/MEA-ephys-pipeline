nextflow.enable.dsl = 2

process job_dispatch {
    tag 'job-dispatch'
    container { task_settings.container }

    input:
    val task_settings
    path local_code, stageAs: 'capsule-source'
    path input_folder, stageAs: 'capsule/data/ecephys_session'

    output:
    // Keyed producer boundary for stage composition; retain the direct ports.
    tuple val(task_settings), val(execution_record), path('unit-results'), path('capsule/data'), path('max_duration.txt'), emit: unit_results
    path 'capsule/results/*', emit: results
    path 'max_duration.txt', emit: max_duration_file  // file containing the value


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
    fi

    TASK_DIR=\$(pwd)

    echo "[${task.tag}] staging local capsule code..."
    cp -rL capsule-source capsule/code
    # Vendored at the original pin; see capsules/job_dispatch/UPSTREAM.md.

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run

    ./run --input nwb ${task_settings.args}


    MAX_DURATION_MIN=\$(python get_max_recording_duration_min.py)

    cd \$TASK_DIR
    echo "\$MAX_DURATION_MIN" > max_duration.txt

    echo "[${task.tag}] completed!"

    """
}
