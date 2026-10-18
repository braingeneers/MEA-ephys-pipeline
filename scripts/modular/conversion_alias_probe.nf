// Test-only real-conversion graph. No scientific process is stubbed.
include { MEA_MAXWELL_CONVERSION as ALPHA; MEA_MAXWELL_CONVERSION as BETA; MEA_MAXWELL_CONVERSION as SINGLE; conversionOwnerVersion; conversionReadJson } from '../../pipeline/conversion'

def captureUnits(rows, String label) {
    file("${label}-channel-units.json").text = groovy.json.JsonOutput.toJson(rows)
    "CHANNEL_UNITS_CAPTURED=${label} ${rows.size()}"
}

workflow {
    def records = conversionReadJson(file(params.recordings_file))
    def bindings = conversionReadJson(file(params.unit_bindings_file))
    def base = [workflow_version:conversionOwnerVersion(),run_id:'two-aliases',
        unit_bindings:bindings,local_bind_paths:params.local_bind_paths]
    if (params.single) {
        SINGLE(channel.value(records),[:],base+[run_id:'single-port',step_id:'single',output_uri:params.single_output])
        SINGLE.out.units.map { meta,path -> [metadata:meta,input:path.toString()] }
            .collect(flat:false).view { rows -> captureUnits(rows,'single') }
        SINGLE.out.completion.view { "SINGLE_COMPLETE=${it}" }
    } else {
    ALPHA(channel.value(records),[:],base+[step_id:'alpha',output_uri:params.alpha_output])
    BETA(channel.value(records),[nwb_chunk_duration_ms:10,create_linked_maxwell_companion:false],
        base+[step_id:'beta',output_uri:params.beta_output])
    ALPHA.out.units.view { meta,path -> "ALPHA_UNIT=${meta.unit_id} ${path}" }
    BETA.out.units.view { meta,path -> "BETA_UNIT=${meta.unit_id} ${path}" }
    ALPHA.out.units.map { meta,path -> [metadata:meta,input:path.toString()] }
        .collect(flat:false).view { rows -> captureUnits(rows,'alpha') }
    BETA.out.units.map { meta,path -> [metadata:meta,input:path.toString()] }
        .collect(flat:false).view { rows -> captureUnits(rows,'beta') }
    ALPHA.out.artifacts.view { "ALPHA_ARTIFACTS=${it}" }
    BETA.out.artifacts.view { "BETA_ARTIFACTS=${it}" }
    ALPHA.out.completion.combine(BETA.out.completion).view { a,b -> "BOTH_COMPLETE=${a} ${b}" }
    }
}
