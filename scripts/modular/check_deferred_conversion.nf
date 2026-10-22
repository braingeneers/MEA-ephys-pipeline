nextflow.enable.dsl = 2
include { conversionContext; conversionSelection; conversionReadJson; conversionOwnerVersion } from '../../pipeline/conversion'

params.binding_case = 'valid'
process DELAYED_SELECTION {
    output: path 'recordings.json'
    script:
    '''
    cat > recordings.json <<'JSON'
    [{"input_uri":"s3://fixture/a/same.raw.h5","data_format":"maxone","unknown":{"nullable":null,"float":7.0,"negative_zero":-0.0,"large":9223372036854775808}},{"input_uri":"s3://fixture/b/same.raw.h5","data_format":"maxtwo","wells":["well001","well002"],"well_filter":["well002"]}]
    JSON
    '''
}
process REQUEST_PROBE {
    input: val request
    output: path 'request.json'
    script:
    """
    cat > request.json <<'JSON'
    ${groovy.json.JsonOutput.toJson(request)}
    JSON
    """
}
workflow {
    def bindings = { records ->
        records.withIndex().collect { record,index ->
            [input_uri:record.input_uri,unit_name:index == 0 ? 'recording' : 'well002',
             metadata:[unit_id:"unit-${index}".toString(),unknown:record.unknown]]
        }
    }
    def context = [output_uri:launchDir.toString(),run_id:'deferred-probe',step_id:'convert',
        workflow_version:conversionOwnerVersion(),unit_bindings:bindings]
    def cfg = conversionContext([:],context)
    assert cfg.wrapper.unit_bindings == [] && cfg.binding_factory.is(bindings)
    assert context.unit_bindings.is(bindings)
    DELAYED_SELECTION()
    def checked = DELAYED_SELECTION.out.map { path ->
        def records = conversionReadJson(path)
        def before = groovy.json.JsonOutput.toJson(records)
        if (params.binding_case == 'invalid-result') cfg.binding_factory = { ignored -> [[:]] }
        if (params.binding_case == 'coverage') cfg.binding_factory = { items -> bindings(items).take(1) }
        if (params.binding_case == 'duplicate') cfg.binding_factory = { items -> bindings(items).collect { it + [metadata:[unit_id:'same']] } }
        if (params.binding_case == 'local') records[0].input_uri = '/fixture/local.raw.h5'
        def request = conversionSelection(cfg,records)
        assert params.binding_case == 'valid'
        assert !request.containsKey('binding_factory')
        assert before == groovy.json.JsonOutput.toJson(records)
        assert cfg.wrapper.unit_bindings == []
        def staticCfg = conversionContext([:],context+[unit_bindings:bindings(records)])
        assert groovy.json.JsonOutput.toJson(conversionSelection(staticCfg,records)) == groovy.json.JsonOutput.toJson(request)
        // Mutation inside the callback cannot edit the authoritative selection.
        def mutated = conversionSelection(cfg+[binding_factory:{ items -> def result=bindings(items); items[0].unknown.float=9.0; result }],records)
        assert before == groovy.json.JsonOutput.toJson(records)
        assert mutated.recordings == records
        request
    }
    REQUEST_PROBE(checked)
    REQUEST_PROBE.out.view { 'REQUEST_FILE='+it }
}
