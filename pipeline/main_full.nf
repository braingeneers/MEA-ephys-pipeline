#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

include { conversionRuntime; conversionOwnerVersion; conversionValue } from './conversion'
include { meaReadJson } from './json_values'
include { loadLaunchSettings } from './settings'
include { MEA_FULL_NWB; MEA_FULL_MAXWELL } from './full'

params.input_mode = 'nwb'
params.ecephys_path = System.getenv('DATA_PATH') ?: System.getenv('DATA_DIR')
params.units_file = null
params.unit_id = null
params.recordings = null
params.recordings_file = null
params.unit_bindings = null
params.unit_bindings_file = null
params.conversion_settings = [:]
params.conversion_settings_file = null
params.local_bind_paths = []
params.native_output_uri = null
params.runtime = [:]
params.run_id = null
params.step_id = 'full'
params.results_path = System.getenv('RESULTS_PATH') ?: "${launchDir}/results"
params.params_file = null
params.torch_device = null
params.publish_overwrite = null
params.git_repo_prefix = System.getenv('GIT_REPO_PREFIX') ?: 'https://github.com/AllenNeuralDynamics/aind-'
params.report_container_image = null
params.container_images = [:]

// The full raw-capable entry has one runtime; existing NWB entries retain NF23.
conversionRuntime()
if (!(params.input_mode in ['nwb','maxwell'])) error 'input_mode must be nwb or maxwell.'
if (!(params.results_path instanceof CharSequence) || !params.results_path.toString().trim() ||
    params.results_path.toString().contains('://')) error 'The portable full entry requires a local POSIX results_path.'
def output = new File(params.results_path.toString()).absolutePath
def context = [output_uri:output,workflow_version:conversionOwnerVersion(),
    run_id:params.run_id ?: java.util.UUID.randomUUID().toString(),step_id:params.step_id]
def rows
def records
def conversion_settings
if (params.input_mode == 'maxwell') {
    if (params.units_file || params.unit_id) error 'Maxwell unit identities belong in unit_bindings; omit units_file and unit_id.'
    records = conversionValue(params.recordings,params.recordings_file,'recordings',false)
    def bindings = conversionValue(params.unit_bindings,params.unit_bindings_file,'unit_bindings',false)
    conversion_settings = conversionValue(params.conversion_settings,params.conversion_settings_file,'conversion_settings',true)
    def runtimeKeys = ['s3_endpoint','aws_profile','public_reader_endpoint','minimum_disk_gib',
        'require_k8s_aws_secret_name','k8s_aws_secret_name'] as Set
    def runtime = params.findAll { key,value -> key in runtimeKeys } + conversionValue(params.runtime,null,'runtime',true)
    context += [unit_bindings:bindings,runtime:runtime,local_bind_paths:params.local_bind_paths,
        native_output_uri:params.native_output_uri]
} else {
    if (params.recordings || params.recordings_file || params.unit_bindings || params.unit_bindings_file ||
        params.conversion_settings || params.conversion_settings_file || params.native_output_uri != null)
        error 'Conversion inputs require input_mode=maxwell.'
    def values
    if (params.units_file) {
        values = meaReadJson(file(params.units_file,checkIfExists:true))
    } else {
        if (!params.unit_id || !params.ecephys_path) error 'Provide unit_id and ecephys_path, or units_file.'
        values = [[metadata:[unit_id:params.unit_id.toString()],input:params.ecephys_path.toString()]]
    }
    if (!(values instanceof List) || !values) error 'units_file must contain a nonempty JSON list.'
    rows = values.collect { row ->
        if (!(row instanceof Map) || !row.input || !(row.metadata instanceof Map)) error 'Each unit needs input and metadata.unit_id.'
        tuple(new LinkedHashMap(row.metadata),file(row.input.toString(),checkIfExists:true))
    }
}
def parsing = new LinkedHashMap(params)
// Stage processes receive their actual path ports; this value only initializes
// the common launch parser before conversion produces those paths.
parsing.ecephys_path = params.input_mode == 'maxwell' ? output : rows[0][1].toString()
def settings = loadLaunchSettings(parsing,baseDir)
if (settings.sorter != 'kilosort4') error 'The named full workflow selects kilosort4; use main_multi_backend.nf for other sorters.'

workflow {
    if (params.input_mode == 'maxwell') {
        MEA_FULL_MAXWELL(channel.value(records),conversion_settings,settings,context)
        finished = MEA_FULL_MAXWELL.out.units
        MEA_FULL_MAXWELL.out.conversion_artifacts.view { "NATIVE_ARTIFACTS=${it}" }
        MEA_FULL_MAXWELL.out.conversion_completion.view { "BRIDGE_COMPLETION=${it}" }
    } else {
        MEA_FULL_NWB(channel.fromList(rows),settings,context)
        finished = MEA_FULL_NWB.out.units
    }
    finished.view { meta,path -> "Verified full unit ${meta.unit_id}: ${path}" }
}
