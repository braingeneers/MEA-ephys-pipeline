// Test-only real-conversion graph. No scientific process is stubbed.
include { MEA_MAXWELL_CONVERSION as ALPHA; MEA_MAXWELL_CONVERSION as BETA; conversionOwnerVersion } from '../../pipeline/conversion'

workflow {
    def records = new groovy.json.JsonSlurper().parse(file(params.recordings_file).toFile())
    def bindings = new groovy.json.JsonSlurper().parse(file(params.unit_bindings_file).toFile())
    def base = [workflow_version:conversionOwnerVersion(),run_id:'two-aliases',
        unit_bindings:bindings,local_bind_paths:params.local_bind_paths]
    ALPHA(channel.value(records),[:],base+[step_id:'alpha',output_uri:params.alpha_output])
    BETA(channel.value(records),[nwb_chunk_duration_ms:10,create_linked_maxwell_companion:false],
        base+[step_id:'beta',output_uri:params.beta_output])
    ALPHA.out.units.view { meta,path -> "ALPHA_UNIT=${meta.unit_id} ${path}" }
    BETA.out.units.view { meta,path -> "BETA_UNIT=${meta.unit_id} ${path}" }
    ALPHA.out.artifacts.view { "ALPHA_ARTIFACTS=${it}" }
    BETA.out.artifacts.view { "BETA_ARTIFACTS=${it}" }
    ALPHA.out.completion.combine(BETA.out.completion).view { a,b -> "BOTH_COMPLETE=${a} ${b}" }
}
