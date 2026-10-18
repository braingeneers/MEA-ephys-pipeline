nextflow.enable.dsl = 2

process verify_stage {
    tag { "${task_settings.unit_id}:${task_settings.stage}" }
    container { task_settings.container }

    input:
    tuple val(task_settings), path(bundle, stageAs: 'input-bundle')
    path helpers, stageAs: 'mea-tools'
    path outcomes_code, stageAs: 'sorting-outcomes.py'

    output:
    tuple val(task_settings), path('verified'), emit: units

    script:
    """
    set -e
    python mea-tools/stage_bundle.py --outcomes-code sorting-outcomes.py verify \
        --bundle input-bundle --kind '${task_settings.stage}' --unit-id '${task_settings.unit_id}'
    ln -s input-bundle verified
    """
}
