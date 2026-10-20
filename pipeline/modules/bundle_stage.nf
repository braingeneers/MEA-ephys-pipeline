nextflow.enable.dsl = 2

process bundle_stage {
    tag { "${task_settings.unit_id}:${task_settings.stage}" }
    container { task_settings.container }
    publishDir { task_settings.results_path }, mode: 'copy', pattern: 'bundle', saveAs: { name -> task_settings.publish && task_settings.publish_overwrite == null ? "${task_settings.unit_id}.${task_settings.stage}" : null }
    publishDir { task_settings.results_path }, mode: 'copy', pattern: 'bundle', overwrite: true, saveAs: { name -> task_settings.publish && task_settings.publish_overwrite == true ? "${task_settings.unit_id}.${task_settings.stage}" : null }
    publishDir { task_settings.results_path }, mode: 'copy', pattern: 'bundle', overwrite: false, saveAs: { name -> task_settings.publish && task_settings.publish_overwrite == false ? "${task_settings.unit_id}.${task_settings.stage}" : null }

    input:
    tuple val(task_settings), val(request), path(payloads, stageAs: 'inputs/item??')
    path helpers, stageAs: 'mea-tools'
    path outcomes_code, stageAs: 'sorting-outcomes.py'
    path common_code, stageAs: 'native-common'

    output:
    tuple val(task_settings), path('bundle'), emit: units

    script:
    """
    set -e
    cat > request.json <<'MEA_REQUEST'
    ${groovy.json.JsonOutput.toJson(request)}
    MEA_REQUEST
    python mea-tools/stage_bundle.py --outcomes-code sorting-outcomes.py --common-code native-common build \
        --request request.json --inputs inputs --output bundle
    """
}
