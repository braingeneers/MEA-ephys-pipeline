nextflow.enable.dsl = 2

process results_collector {
    tag 'result-collector'
    container { task_settings.container }

    // Keep option booleans static for Nextflow 23; saveAs selects one policy.
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', saveAs: { filename -> task_settings.publish_overwrite == null ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: true, saveAs: { filename -> task_settings.publish_overwrite == true ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: false, saveAs: { filename -> task_settings.publish_overwrite == false ? new File(filename).getName() : null }

    input:
    val task_settings
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

    echo "[${task.tag}] cloning git repo..."
    ${task_settings.clone_script}
    clone_repo "${task_settings.git_repo_prefix}ephys-results-collector.git" "${task_settings.capsule_pin}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run --pipeline-data-path ${task_settings.input_path} --pipeline-results-path ${task_settings.results_path}

    echo "[${task.tag}] completed!"
    """
}
