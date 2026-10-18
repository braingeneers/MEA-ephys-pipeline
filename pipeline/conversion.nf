nextflow.enable.dsl = 2

include { PREPARE_MAXWELL_SELECTION; MAXWELL_TO_NWB; maxwellMetadata } from './components/maxwell/component'

def conversionRuntime() {
    if (nextflow.version.toString() != '26.04.6') error 'Maxwell conversion requires Nextflow 26.04.6; existing NWB/NERSC entries retain their own runtime.'
    try {
        this.class.classLoader.loadClass('MaxwellTaskRetry')
    } catch (ClassNotFoundException ignored) {
        error 'Load the owning conversion retry library with: nextflow run <entry> -lib <pipeline>/components/maxwell/lib'
    }
}

def conversionVersion(path) {
    def match = (file(path).text =~ /(?m)^\s*version\s*=\s*['"]([^'"]+)['"]/)
    if (!match.find()) error 'An owning workflow requires a literal version.'
    match.group(1)
}

def conversionOwnerVersion() { conversionVersion("${moduleDir}/nextflow.config") }

def conversionCopy(value) {
    if (value instanceof Map) {
        def result = new LinkedHashMap()
        value.each { key,item -> result[key] = conversionCopy(item) }
        return result
    }
    if (value instanceof List) return value.collect { conversionCopy(it) }
    if (value instanceof GString) return value.toString()
    value
}

def conversionSnapshot(value) {
    // JsonSlurper LazyMap materialization mutates internal state. A shared input
    // must be copied under its own lock before aliases serialize it concurrently.
    synchronized (value) {
        groovy.json.JsonOutput.toJson(value)  // Validate JSON without changing numeric encodings.
        conversionCopy(value)
    }
}

// This separate module deliberately does not enter the five-stage Nextflow 23 path.
def conversionContext(Map settings, Map context) {
    conversionRuntime()
    settings = conversionSnapshot(settings)
    context = conversionSnapshot(context)
    def allowed = ['output_uri','workflow_version','run_id','step_id','unit_bindings','runtime','local_bind_paths'] as Set
    if (context.keySet() - allowed) error 'Unknown conversion context; source replacement/finalization is unsupported.'
    def nativeRoot = "${moduleDir}/components/maxwell"
    def defaults = new groovy.json.JsonSlurper().parse(file("${nativeRoot}/defaults.json").toFile())
    if (settings.keySet() - defaults.keySet()) error 'Unknown conversion settings; source replacement/finalization is unsupported.'
    def ownerVersion = conversionOwnerVersion()
    if (context.workflow_version != ownerVersion) error 'Conversion context must retain the owning MEA version.'
    for (key in ['run_id','step_id']) {
        if (!(context[key] instanceof String) || !(context[key] ==~ /[A-Za-z0-9][A-Za-z0-9_.-]{0,159}/))
            error "Conversion context requires a stable path-safe ${key}."
    }
    if (!(context.output_uri instanceof String)) error 'Conversion output_uri must be an existing absolute POSIX directory or file URI.'
    def directory = context.output_uri.startsWith('file:///') ? new File(new URI(context.output_uri)) : new File(context.output_uri)
    if (!directory.isAbsolute() || !directory.isDirectory() || context.output_uri.startsWith('s3:'))
        error 'Portable conversion requires an existing absolute local output directory.'
    def output = directory.toPath().normalize().toUri().toString()
    if (!output.endsWith('/')) output += '/'
    if (!(context.unit_bindings instanceof List) || !context.unit_bindings) error 'Explicit unit_bindings are required.'
    def ids = []
    context.unit_bindings.each { binding ->
        if (!(binding instanceof Map) || binding.keySet() != (['input_uri','unit_name','metadata'] as Set) ||
            !(binding.metadata instanceof Map) || !(binding.metadata.unit_id instanceof String) ||
            !(binding.metadata.unit_id ==~ /[A-Za-z0-9][A-Za-z0-9_.-]{0,159}/) || binding.metadata.containsKey('maxwell_conversion'))
            error 'Each unit binding requires input_uri, unit_name and metadata with an unreserved stable unit_id.'
        ids.add(binding.metadata.unit_id)
    }
    if (ids.toSet().size() != ids.size()) error 'Conflicting conversion unit identities.'
    def runtimeKeys = ['s3_endpoint','aws_profile','public_reader_endpoint','minimum_disk_gib',
        'require_k8s_aws_secret_name','k8s_aws_secret_name'] as Set
    if (context.runtime != null && (!(context.runtime instanceof Map) || context.runtime.keySet() - runtimeKeys))
        error 'Conversion runtime contains unsupported options; source replacement/finalization is unsupported.'
    if (context.local_bind_paths != null && !(context.local_bind_paths instanceof List)) error 'local_bind_paths must be a list.'
    def localPaths = context.unit_bindings.collect { it.input_uri }.findAll { it instanceof String && !it.startsWith('s3://') }
        .collect { file(it,checkIfExists:true).parent.toString() }
    localPaths += (context.local_bind_paths ?: []).collect { file(it,checkIfExists:true).toString() }
    def image = new groovy.json.JsonSlurper().parse(file("${nativeRoot}/image.json").toFile()).pinned_reference
    def distribution = new groovy.json.JsonSlurper().parse(file("${nativeRoot}/export-manifest.json").toFile())
    def nativeContext = [output_base_uri:output,output_uri:output,run_id:context.run_id,step_id:context.step_id,
        workflow_version:conversionVersion("${nativeRoot}/nextflow.config"),runtime:new LinkedHashMap(context.runtime ?: [:]),
        local_bind_paths:(localPaths+[directory.toString()]).unique()]
    def metadata = maxwellMetadata(settings,nativeContext,image)
    [native:[context:nativeContext,owner:metadata.owner,defaults:defaults,
             distribution:[source_commit:distribution.source_commit,content_sha256:distribution.content_sha256]],
     wrapper:[owner:[workflow_id:'MEA-ephys-pipeline',workflow_version:ownerVersion],
              publication_root:output+"bridge/${context.run_id}/${context.step_id}/",
              unit_bindings:context.unit_bindings],settings:new LinkedHashMap(settings),image:image]
}

def conversionSelection(Map request, records) {
    if (!(records instanceof List) || !records) error 'Expected one nonempty complete recordings selection.'
    records = conversionSnapshot(records)
    def normalize = { value ->
        if (!(value instanceof String) || !value) error 'Explicit recording/binding input_uri is required.'
        if (value.startsWith('s3://') || value.startsWith('file:///')) return value
        if (!new File(value).isAbsolute() || value.contains('://')) error 'Local conversion inputs must be absolute POSIX paths or file URIs.'
        new File(value).toPath().normalize().toUri().toString()
    }
    def wells = { value ->
        if (value == null) return []
        if (!(value instanceof List || value instanceof String)) error 'Wells must be a list or comma-separated names.'
        def names = value instanceof List ? value : value.split(',').toList()
        names.collect { it.toString().trim() }.findAll { it }
    }
    def expected = []
    def inputs = []
    records.each { record ->
        if (!(record instanceof Map)) error 'Every recording must be a complete dictionary.'
        def uri = normalize(record.input_uri)
        inputs.add(uri)
        (record.nwb_metadata ?: [:]).values().each { projection ->
            def location = normalize(projection.uri)
            if (!location.startsWith('s3://')) {
                def path = new File(new URI(location)).toPath()
                if (!request.native.context.local_bind_paths.any { binding -> path == new File(binding).toPath() ||
                    (new File(binding).isDirectory() && path.startsWith(new File(binding).toPath())) })
                    error 'Supply local_bind_paths for projections outside the original source directories.'
            }
        }
        def names = record.data_format?.toLowerCase() in ['maxtwo','max2'] ? (wells(record.well_filter) ?: wells(record.wells)) : ['recording']
        if (!names) error 'Max Two requires selected uploaded wells.'
        names.each { name -> expected.add([uri,name]) }
    }
    def given = request.wrapper.unit_bindings.collect { [normalize(it.input_uri),it.unit_name] }
    if (inputs.unique(false).size() != inputs.size() || given.toSet().size() != given.size() || given.toSet() != expected.toSet())
        error 'Explicit unit bindings must cover the selected recording URIs/wells exactly once.'
    request + [recordings:records]
}

process MEA_MAXWELL_PREPARE_UNITS {
    tag { cfg.native.context.step_id }
    container { cfg.image }
    cpus 1
    memory '4 GB'
    disk '5 GB'
    time '4h'
    errorStrategy 'terminate'
    input:
    tuple val(cfg), val(artifacts)
    path bridge
    path runtime
    path common
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    tuple val(cfg), path('bridge-plan.json'), emit: plan
    script:
    """
    cat > bridge-request.json <<'MEA_CONVERSION_REQUEST'
    ${groovy.json.JsonOutput.toJson(cfg)}
    MEA_CONVERSION_REQUEST
    PYTHONPATH="\$PWD/${runtime}:\$PWD/${common}:/opt/ephys-modular/maxwell/scripts" python '${bridge}' prepare --request bridge-request.json --artifacts '${artifacts}'
    """
}

process MEA_MAXWELL_MATERIALIZE_UNIT {
    tag { "${meta.step_id}/${meta.unit_id}" }
    container { meta.image }
    cpus 1
    memory '4 GB'
    disk { meta.requested_bytes as long }
    time '4h'
    errorStrategy 'terminate'
    input:
    tuple val(meta), path(plan)
    path bridge
    path runtime
    path common
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    tuple val(meta), path('unit'), emit: units
    path 'materialization-telemetry.json', emit: telemetry
    script:
    """
    PYTHONPATH="\$PWD/${runtime}:\$PWD/${common}:/opt/ephys-modular/maxwell/scripts" python '${bridge}' materialize --plan '${plan}' --unit-id '${meta.unit_id}' --requested-bytes '${task.disk.toBytes()}'
    """
}

process MEA_MAXWELL_COMMIT_UNITS {
    tag { cfg.native.context.step_id }
    container { cfg.image }
    cpus 1
    memory '4 GB'
    disk { cfg.commit_disk_bytes as long }
    time '4h'
    errorStrategy 'terminate'
    input:
    tuple val(cfg), path(plan)
    path units, stageAs: 'materialized/unit??'
    path bridge
    path runtime
    path common
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    path 'bridge-result.json', emit: result
    script:
    """
    PYTHONPATH="\$PWD/${runtime}:\$PWD/${common}:/opt/ephys-modular/maxwell/scripts" python '${bridge}' commit --plan '${plan}' --units materialized
    """
}

workflow MEA_MAXWELL_CONVERSION {
    take: recordings; settings; context
    main:
    def cfg = conversionContext(settings,context)
    selection = recordings.toList().map { values ->
        if (values.size() != 1) error 'Expected one complete recordings selection.'
        conversionSelection(cfg,values[0])
    }
    def inputBindings = cfg.wrapper.unit_bindings.collect { it.input_uri }
    // Explicit paths supply Docker/local transport visibility, never unit identity.
    def paths = inputBindings.findAll { !it.startsWith('s3://') }.collect { file(it,checkIfExists:true) }
    def root = file(cfg.native.context.output_base_uri,checkIfExists:true)
    // Projections are carried with the complete recording dictionaries.
    projectionBindings = selection.map { request ->
        request.recordings.collectMany { (it.nwb_metadata ?: [:]).values().collect { projection -> projection.uri } }
            .findAll { it && !it.startsWith('s3://') }.collect { file(it,checkIfExists:true) }
    }
    localBindings = projectionBindings.map { projections -> (paths+[root]+projections).unique() }
    // Native exports take a value list; their context bindings must be static.
    // Complete parent directories make projection/source URIs visible in local tasks.
    def nativeContext = cfg.native.context
    PREPARE_MAXWELL_SELECTION(selection.map { it.recordings },cfg.settings,nativeContext +
        [step_id:nativeContext.step_id+'-raw',collection_uri:cfg.native.context.output_base_uri+'derived/nwb/'])
    MAXWELL_TO_NWB(PREPARE_MAXWELL_SELECTION.out.artifacts,cfg.settings,nativeContext)
    def bridge = file("${moduleDir}/helpers/maxwell_bridge.py")
    def runtime = file("${moduleDir}/components/maxwell/runtime")
    def common = file("${moduleDir}/components/maxwell/components/common/python")
    MEA_MAXWELL_PREPARE_UNITS(MAXWELL_TO_NWB.out.artifacts.combine(selection).map { uri,request -> tuple(request,uri) },
        bridge,runtime,common,localBindings)
    calls = MEA_MAXWELL_PREPARE_UNITS.out.plan.flatMap { request,plan ->
        def value = new groovy.json.JsonSlurper().parse(plan.toFile())
        value.units.collect { unit -> tuple([unit_id:unit.unit_meta.unit_id,step_id:nativeContext.step_id,
            image:cfg.image,requested_bytes:unit.requested_bytes],plan) }
    }
    MEA_MAXWELL_MATERIALIZE_UNIT(calls,bridge,runtime,common,localBindings)
    commitPlan = MEA_MAXWELL_PREPARE_UNITS.out.plan.map { request,plan ->
        def value = new groovy.json.JsonSlurper().parse(plan.toFile())
        tuple(request+[commit_disk_bytes:value.commit_disk_bytes],plan)
    }
    finished = MEA_MAXWELL_MATERIALIZE_UNIT.out.units.collect(flat:false).map { rows -> rows.sort { a,b -> a[0].unit_id <=> b[0].unit_id }.collect { it[1] } }
    MEA_MAXWELL_COMMIT_UNITS(commitPlan,finished,bridge,runtime,common,localBindings)
    completed = MEA_MAXWELL_COMMIT_UNITS.out.result.map { new groovy.json.JsonSlurper().parse(it.toFile()) }
    emit:
    units = completed.flatMap { result -> result.units.collect { unit -> tuple(unit.metadata,file(unit.input,checkIfExists:true)) } }
    artifacts = completed.map { it.native_artifacts.uri }
    completion = completed.map { it.completion.uri }
}
