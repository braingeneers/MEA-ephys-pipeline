nextflow.enable.dsl = 2

include { meaCodeDigest } from '../code_identity'

process bundle_stage {
    tag { "${task_settings.unit_id}:${task_settings.stage}" }
    container { task_settings.container }
    // This is the shared output handoff. Preserve atomic manifest publication
    // instead of copying it in place from a separate scratch filesystem.
    scratch false
    publishDir { task_settings.results_path }, mode: 'copy', pattern: 'bundle', saveAs: { name -> task_settings.publish && task_settings.publish_overwrite == null ? "${task_settings.unit_id}.${task_settings.stage}" : null }
    publishDir { task_settings.results_path }, mode: 'copy', pattern: 'bundle', overwrite: true, saveAs: { name -> task_settings.publish && task_settings.publish_overwrite == true ? "${task_settings.unit_id}.${task_settings.stage}" : null }
    publishDir { task_settings.results_path }, mode: 'copy', pattern: 'bundle', overwrite: false, saveAs: { name -> task_settings.publish && task_settings.publish_overwrite == false ? "${task_settings.unit_id}.${task_settings.stage}" : null }

    input:
    tuple val(task_settings), val(request), path(payloads, stageAs: 'inputs/item??')
    tuple val(helpers_identity), path(helpers, stageAs: 'mea-tools')
    tuple val(outcomes_code_identity), path(outcomes_code, stageAs: 'sorting-outcomes.py')
    tuple val(common_code_identity), path(common_code, stageAs: 'native-common')

    output:
    tuple val(task_settings), path('bundle'), emit: units

    script:
    def staged_code_identity = meaCodeDigest([helpers, outcomes_code, common_code])
    """
    # MEA staged-code SHA-256: ${staged_code_identity}
    set -e
    cat > request.json <<'MEA_REQUEST'
    ${groovy.json.JsonOutput.toJson(request)}
    MEA_REQUEST
    python mea-tools/stage_bundle.py --outcomes-code sorting-outcomes.py --common-code native-common build \
        --request request.json --inputs inputs --output bundle
    """
}
