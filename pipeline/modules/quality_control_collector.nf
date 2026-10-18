nextflow.enable.dsl = 2

process quality_control_collector {
    tag 'qc-collector'
    container { task_settings.container }

    // Keep option booleans static for Nextflow 23; saveAs selects one policy.
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', saveAs: { filename -> task_settings.publish_overwrite == null ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: true, saveAs: { filename -> task_settings.publish_overwrite == true ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: false, saveAs: { filename -> task_settings.publish_overwrite == false ? new File(filename).getName() : null }

    input:
    val task_settings
    val max_duration_minutes
    path quality_control_results, stageAs: 'capsule/data/*'

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
    fi

    echo "[${task.tag}] cloning git repo..."
    ${task_settings.clone_script}
    clone_repo "${task_settings.git_repo_prefix}ephys-qc-collector.git" "${task_settings.capsule_pin}"

    echo "[${task.tag}] running capsule..."
    cd capsule/code
    chmod +x run
    ./run

    echo "[${task.tag}] completed!"
    """
}
