include { PREPARE_MAXWELL_SELECTION; MAXWELL_TO_NWB } from './component'

workflow {
    def runId = params.run_id ?: java.util.UUID.randomUUID().toString()
    def root = params.output_uri
    if (!root) error 'An explicit output_uri is required'
    if (!root.startsWith('s3://')) {
        def directory = file(root)
        directory.mkdirs()
        root = directory.toUri().toString()
    }
    if (!root.endsWith('/')) root += '/'
    def localPaths = []
    if (root.startsWith('file:///')) localPaths.add(root)
    def records = params.recordings
    if (records instanceof String) records = new groovy.json.JsonSlurper().parseText(records)
    if (!params.artifacts) {
        if (!(records instanceof List) || !records) error 'Provide artifacts or a nonempty recordings array'
        records.each { record ->
            def inputs = [record.input_uri] + (record.nwb_metadata ?: [:]).values().collect { it.uri }
            inputs.findAll { it && !it.startsWith('s3://') }.each { localPaths.add(it) }
        }
    }
    def settings = params.settings
    if (settings instanceof String) settings = new groovy.json.JsonSlurper().parseText(settings)
    def context = [output_base_uri: root, output_uri: root, run_id: runId,
        step_id: params.step_id, workflow_version: workflow.manifest.version,
        runtime: params, local_bind_paths: (localPaths + (params.local_bind_paths ?: [])).unique()]
    def input = channel.value(params.artifacts)
    if (!params.artifacts) {
        if (!params.collection_uri) error 'Raw selection requires an explicit collection_uri within output_uri'
        PREPARE_MAXWELL_SELECTION(channel.value(records), settings, context + [step_id: params.step_id + '-raw',
            collection_uri: params.collection_uri, cache_uri: params.cache_uri])
        input = PREPARE_MAXWELL_SELECTION.out.artifacts
    }
    MAXWELL_TO_NWB(input, settings, context)
    MAXWELL_TO_NWB.out.artifacts.view { uri -> "ARTIFACTS_URI=${uri}" }
}
