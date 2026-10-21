nextflow.enable.dsl = 2

include { MEA_MAXWELL_CONVERSION } from './conversion'
include { MEA_PREPROCESSING; MEA_KILOSORT4; MEA_ANALYSIS; MEA_REPORTING; MEA_FINAL_EXPORT; stageContext } from './stages'

def fullStageContext(Map context, String stage) {
    def owner = stageContext(context)
    owner + [output_uri:"${owner.output_uri}/${stage}".toString(),
             step_id:"${owner.step_id ?: 'full'}-${stage}".toString()]
}

// The same maintained stages used by independent callers; no duplicate science.
workflow MEA_FULL_NWB {
    take: units; settings; context
    main:
    MEA_PREPROCESSING(units,settings,fullStageContext(context,'preprocessing'))
    MEA_KILOSORT4(MEA_PREPROCESSING.out.units,settings,fullStageContext(context,'sorting'))
    MEA_ANALYSIS(MEA_KILOSORT4.out.units,settings,fullStageContext(context,'analysis'))
    MEA_REPORTING(MEA_ANALYSIS.out.units,settings,fullStageContext(context,'reporting'))
    MEA_FINAL_EXPORT(MEA_REPORTING.out.units,settings,fullStageContext(context,'export'))
    emit:
    units = MEA_FINAL_EXPORT.out.units
}

workflow MEA_FULL_MAXWELL {
    take: recordings; conversion_settings; settings; context
    main:
    def conversion_context = context + fullStageContext(context,'conversion')
    MEA_MAXWELL_CONVERSION(recordings,conversion_settings,conversion_context)
    MEA_FULL_NWB(MEA_MAXWELL_CONVERSION.out.units,settings,context)
    emit:
    units = MEA_FULL_NWB.out.units
    conversion_artifacts = MEA_MAXWELL_CONVERSION.out.artifacts
    conversion_completion = MEA_MAXWELL_CONVERSION.out.completion
}
