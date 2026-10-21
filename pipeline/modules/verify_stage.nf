nextflow.enable.dsl = 2

include { meaCodeDigest } from '../code_identity'

process verify_stage {
    tag { "${task_settings.unit_id}:${task_settings.stage}" }
    container { task_settings.container }

    input:
    tuple val(task_settings), path(bundle, stageAs: 'input-bundle')
    tuple val(helpers_identity), path(helpers, stageAs: 'mea-tools')
    tuple val(outcomes_code_identity), path(outcomes_code, stageAs: 'sorting-outcomes.py')
    tuple val(common_code_identity), path(common_code, stageAs: 'native-common')

    output:
    tuple val(task_settings), path('verified'), emit: units

    script:
    def staged_code_identity = meaCodeDigest([helpers, outcomes_code, common_code])
    """
    # MEA staged-code SHA-256: ${staged_code_identity}
    set -e
    cat > unit-meta.json <<'MEA_UNIT_META'
    ${groovy.json.JsonOutput.toJson(task_settings.unit_meta)}
    MEA_UNIT_META
    python mea-tools/stage_bundle.py --outcomes-code sorting-outcomes.py --common-code native-common verify \
        --bundle input-bundle --kind '${task_settings.stage}' --unit-id '${task_settings.unit_id}' --unit-meta unit-meta.json
    ln -s input-bundle verified
    """
}
