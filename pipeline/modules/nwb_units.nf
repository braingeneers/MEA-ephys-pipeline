nextflow.enable.dsl = 2

process nwb_units {
    tag 'nwb-units'
    container { task_settings.container }

    // Keep option booleans static for Nextflow 23; saveAs selects one policy.
    publishDir { "${task_settings.results_path}/nwb" }, mode: 'copy', pattern: 'capsule/results/*', saveAs: { filename -> task_settings.publish_overwrite == null ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}/nwb" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: true, saveAs: { filename -> task_settings.publish_overwrite == true ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}/nwb" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: false, saveAs: { filename -> task_settings.publish_overwrite == false ? new File(filename).getName() : null }

    input:
    val task_settings
    path local_code, stageAs: 'capsule-source'
    val max_duration_minutes
    path ecephys_session_input, stageAs: 'capsule/data/ecephys_session'
    path job_dispatch_results, stageAs: 'capsule/data/*'
    path results_data, stageAs: 'capsule/data/*'
    path nwb_ecephys_results, stageAs: 'capsule/data/*'

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

    echo "[${task.tag}] staging local capsule code..."
    cp -rL capsule-source capsule/code
    # Vendored at the original pin; see capsules/nwb_units/UPSTREAM.md.

    if [[ ${task_settings.executor_mode} == "slurm" ]]; then
        echo "[${task.tag}] allocated task time: ${task.time}"
    fi

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run

    echo "[${task.tag}] completed!"
    """
}
