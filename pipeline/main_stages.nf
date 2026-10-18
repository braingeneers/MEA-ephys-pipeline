#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

include { loadLaunchSettings } from './settings'
include { MEA_PREPROCESSING; MEA_KILOSORT4; MEA_ANALYSIS; MEA_REPORTING; MEA_FINAL_EXPORT } from './stages'

params.stage = 'all'
params.units_file = null
params.unit_id = null
params.ecephys_path = System.getenv('DATA_PATH') ?: System.getenv('DATA_DIR')
params.input_bundle = null
params.results_path = System.getenv('RESULTS_PATH') ?: "${launchDir}/results"
params.params_file = null
params.torch_device = null
params.publish_overwrite = null
params.git_repo_prefix = System.getenv('GIT_REPO_PREFIX') ?: 'https://github.com/AllenNeuralDynamics/aind-'
params.report_container_image = null
params.container_images = [:]

def selected = params.stage.toString()
if (!(selected in ['all','preprocessing','sorting','analysis','reporting','export'])) error 'stage must be all, preprocessing, sorting, analysis, reporting or export.'
def input_rows
if (params.units_file) {
    input_rows = new groovy.json.JsonSlurper().parseText(file(params.units_file,checkIfExists:true).text)
    if (!(input_rows instanceof List) || !input_rows) error 'units_file must contain a non-empty JSON list.'
} else {
    if (!params.unit_id) error 'Set --unit_id to a stable identity, or use --units_file.'
    def input_path = selected in ['all','preprocessing'] ? params.ecephys_path : params.input_bundle
    if (!input_path) error 'Provide --ecephys_path for preprocessing/all or --input_bundle for a downstream stage.'
    def meta = [unit_id:params.unit_id.toString()]
    if (!(selected in ['all','preprocessing'])) {
        def prior = new groovy.json.JsonSlurper().parseText(file("${input_path}/bundle.json",checkIfExists:true).text)
        meta = (prior.metadata.unit_meta ?: [:]) + meta
    }
    input_rows = [[input:input_path.toString(),metadata:meta]]
}
def rows = input_rows.collect { row ->
    if (!(row instanceof Map) || !row.input || !(row.metadata instanceof Map)) error 'Each units_file row requires input and metadata.unit_id.'
    def path = file(row.input.toString(),checkIfExists:true)
    def meta = new LinkedHashMap(row.metadata)
    if (!(selected in ['all','preprocessing'])) {
        def prior = new groovy.json.JsonSlurper().parseText(path.resolve('bundle.json').text)
        meta = (prior.metadata.unit_meta ?: [:]) + meta
    }
    tuple(meta,path)
}
def parsing = new LinkedHashMap(params)
parsing.ecephys_path = params.ecephys_path ?: rows[0][1].toString()
launch_settings = loadLaunchSettings(parsing,baseDir)
def version_match = (file("${baseDir}/nextflow.config").text =~ /(?m)^\s*version\s*=\s*['"]([^'"]+)['"]/)
if (!version_match.find()) error 'The owning workflow needs a literal manifest.version.'
owner_context = [workflow_version:version_match.group(1),output_uri:params.results_path.toString()]

workflow {
    current = channel.fromList(rows)
    if (selected in ['all','preprocessing']) {
        MEA_PREPROCESSING(current,launch_settings,owner_context+[step_id:'preprocessing',output_uri:"${owner_context.output_uri}/preprocessing"])
        current = MEA_PREPROCESSING.out.units
    }
    if (selected in ['all','sorting']) {
        MEA_KILOSORT4(current,launch_settings,owner_context+[step_id:'sorting',output_uri:"${owner_context.output_uri}/sorting"])
        current = MEA_KILOSORT4.out.units
    }
    if (selected in ['all','analysis']) {
        MEA_ANALYSIS(current,launch_settings,owner_context+[step_id:'analysis',output_uri:"${owner_context.output_uri}/analysis"])
        current = MEA_ANALYSIS.out.units
    }
    if (selected in ['all','reporting']) {
        MEA_REPORTING(current,launch_settings,owner_context+[step_id:'reporting',output_uri:"${owner_context.output_uri}/reporting"])
        current = MEA_REPORTING.out.units
    }
    if (selected in ['all','export']) {
        MEA_FINAL_EXPORT(current,launch_settings,owner_context+[step_id:'export',output_uri:"${owner_context.output_uri}/export"])
        current = MEA_FINAL_EXPORT.out.units
    }
    current.view{ meta,path -> "Verified ${selected} unit ${meta.unit_id}: ${path}" }
}
