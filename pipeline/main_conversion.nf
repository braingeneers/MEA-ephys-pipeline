#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

include { MEA_MAXWELL_CONVERSION; conversionRuntime; conversionOwnerVersion } from './conversion'

params.recordings = null
params.recordings_file = null
params.unit_bindings = null
params.unit_bindings_file = null
params.settings = [:]
params.settings_file = null
params.output_uri = null
params.run_id = null
params.step_id = 'convert'
params.local_bind_paths = []
params.runtime = [:]

def conversionValue(value, filename, String name, boolean object) {
    if (filename != null) {
        if (value != null && !(object && value instanceof Map && value.isEmpty()))
            error "Provide ${name} or ${name}_file, not both."
        value = new groovy.json.JsonSlurper().parse(file(filename.toString(),checkIfExists:true).toFile())
    } else if (value instanceof String) {
        value = new groovy.json.JsonSlurper().parseText(value)
    }
    if (object ? !(value instanceof Map) : !(value instanceof List) || !value)
        error "${name} must be ${object ? 'a JSON object' : 'a nonempty JSON array'}."
    value
}

workflow {
    conversionRuntime()
    def records = conversionValue(params.recordings,params.recordings_file,'recordings',false)
    def bindings = conversionValue(params.unit_bindings,params.unit_bindings_file,'unit_bindings',false)
    def settings = conversionValue(params.settings,params.settings_file,'settings',true)
    def runtimeKeys = ['s3_endpoint','aws_profile','public_reader_endpoint','minimum_disk_gib',
        'require_k8s_aws_secret_name','k8s_aws_secret_name'] as Set
    def runtime = params.findAll { key,value -> key in runtimeKeys } + conversionValue(params.runtime,null,'runtime',true)
    if (!params.output_uri) error 'Provide output_uri as an existing local output directory.'
    def context = [output_uri:params.output_uri.toString(),workflow_version:conversionOwnerVersion(),
        run_id:params.run_id ?: java.util.UUID.randomUUID().toString(),step_id:params.step_id,
        unit_bindings:bindings,runtime:runtime,local_bind_paths:params.local_bind_paths]
    MEA_MAXWELL_CONVERSION(channel.value(records),settings,context)
    MEA_MAXWELL_CONVERSION.out.units.view { metadata,path -> "LOCAL_UNIT=${metadata.unit_id} ${path}" }
    MEA_MAXWELL_CONVERSION.out.artifacts.view { "NATIVE_ARTIFACTS=${it}" }
    MEA_MAXWELL_CONVERSION.out.completion.view { "BRIDGE_COMPLETION=${it}" }
}
