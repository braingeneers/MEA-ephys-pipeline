nextflow.enable.dsl = 2

include { meaCodeDigest } from '../code_identity'

process report_generation {
    tag { "report-generation:${recording_id}" }
    container { task_settings.container }
    // Keep option booleans static for Nextflow 23; saveAs selects one policy.
    publishDir { "${task_settings.results_path}/reports" }, mode: 'copy', pattern: 'capsule/results/*', saveAs: { filename -> task_settings.publish_overwrite == null ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}/reports" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: true, saveAs: { filename -> task_settings.publish_overwrite == true ? new File(filename).getName() : null }
    publishDir { "${task_settings.results_path}/reports" }, mode: 'copy', pattern: 'capsule/results/*', overwrite: false, saveAs: { filename -> task_settings.publish_overwrite == false ? new File(filename).getName() : null }

    input:
    val task_settings
    tuple val(recording_id), path(analyzer, stageAs: 'capsule/data/analyzer.zarr'), path(recording_dependencies, stageAs: 'capsule/recording-dependencies/input??/*')
    tuple val(report_code_identity), path(report_code, stageAs: 'capsule/code')

    output:
    tuple val(task_settings), val(execution_record), val(recording_id), path('unit-results'), path('capsule/data'), emit: unit_results
    tuple val(recording_id), path('capsule/results/*'), emit: results

    script:
    def staged_code_identity = meaCodeDigest([report_code])
    execution_record = [container: task.container ?: '', cpus: task.cpus,
        memory: task.memory?.toString(), time: task.time?.toString(), disk: task.disk?.toString(),
        accelerator: task.accelerator ? [request: task.accelerator.request, limit: task.accelerator.limit] : null]
    // Named stages stage the whole verified bundle so lazy recording dependencies
    // remain in the same Docker mount. Direct callers declare every raw/prepared
    // dependency too; analyzer serialization alone is not a container path input.
    def analyzer_dir = 'capsule/data/analyzer.zarr' + (task_settings.analyzer_subpath ? "/${task_settings.analyzer_subpath}" : '')
    """
    #!/usr/bin/env bash
    # MEA staged-code SHA-256: ${staged_code_identity}
    set -e
    ln -s capsule/results unit-results
    mkdir -p 'capsule/results/${recording_id}'
    python capsule/code/run_capsule.py \
        --analyzer-dir '${analyzer_dir}' \
        --output-dir 'capsule/results/${recording_id}' \
        --thresholds '{"firing_rate": 0.1, "presence_ratio": 0.8}'
    """
}
