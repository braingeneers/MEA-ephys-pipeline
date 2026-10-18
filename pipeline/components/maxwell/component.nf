nextflow.enable.dsl = 2

// Nextflow's bundled Jackson reader preserves JSON integer/floating encodings and
// IEEE negative zero. JsonSlurper's BigDecimal reader loses the latter on input.
def maxwellJson(String text) {
    def mapper = new com.fasterxml.jackson.databind.ObjectMapper()
    mapper.enable(com.fasterxml.jackson.databind.DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
    mapper.enable(com.fasterxml.jackson.core.JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
    def value = mapper.readValue(text, Object.class)
    maxwellFiniteJson(value)
    value
}

def maxwellFiniteJson(value) {
    if (value instanceof Map) value.values().each { maxwellFiniteJson(it) }
    else if (value instanceof List) value.each { maxwellFiniteJson(it) }
    else if (value instanceof Double && !Double.isFinite(value))
        error 'Maxwell JSON floating values must be finite binary64 numbers.'
}

// Each alias gets an independent metadata value. Global params are deployment only.
def maxwellMetadata(settings, context, image) {
    if (!(settings instanceof Map)) error 'Maxwell settings must be an object'
    def submitted = new LinkedHashMap(settings)
    // The portal exposes one JSON object; portable callers may pass the map directly.
    if (submitted.keySet() == (['settings'] as Set)) {
        if (!(submitted.settings instanceof Map)) error 'The settings parameter must be an object'
        submitted = new LinkedHashMap(submitted.settings)
    }
    def runtimeKeys = ['s3_endpoint', 'aws_profile', 'public_reader_endpoint', 'minimum_disk_gib',
        'require_k8s_aws_secret_name', 'k8s_aws_secret_name']
    def runtime = new LinkedHashMap(context.runtime ?: [:]).findAll { k, v -> runtimeKeys.contains(k) }
    if (runtime.require_k8s_aws_secret_name && !runtime.k8s_aws_secret_name) error 'A Kubernetes AWS Secret name is required'
    if (!context.run_id || !context.step_id || !context.workflow_version) error 'Execution and owning workflow identities are required'
    def root = context.output_base_uri ?: context.output_uri
    if (!(root instanceof String) || !root.endsWith('/') || !(root.startsWith('s3://') || root.startsWith('file:///')))
        error 'Output must be an explicit s3:// or file:/// prefix ending in /'
    if (root.startsWith('s3://') && !runtime.public_reader_endpoint)
        error 'S3 output requires an explicit public_reader_endpoint for LINDI references'
    def execution = [output_base_uri: root, run_id: context.run_id, step_id: context.step_id,
        workflow_version: context.workflow_version, runtime: runtime]
    if (context.collection_uri) execution.collection_uri = context.collection_uri
    if (context.cache_uri) execution.cache_uri = context.cache_uri
    return [owner: [workflow_id: 'braingeneers-modular-maxwell-to-nwb',
        workflow_version: context.workflow_version, interface_version: '1', image: image,
        maintainer: 'Ephys workflow maintainers'], context: execution, runtime: runtime,
        settings: submitted, transport: root.startsWith('s3://') ? 's3' : 'local']
}

def maxwellPayload(meta, task, input = [:]) {
    return groovy.json.JsonOutput.toJson(input + [meta: meta, attempt: task.attempt,
        resources: [cpus: task.cpus, memory_bytes: task.memory.toBytes(),
            disk_bytes: task.disk.toBytes(), time_millis: task.time.toMillis()]])
}

process MAXWELL_RAW_SELECTION {
    tag { meta.context.step_id }
    container { meta.owner.image }
    cpus 1
    memory '16 GB'
    disk '80 GB'
    time '24h'
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    input:
    tuple val(meta), val(recordings)
    path runtime
    path common
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    path 'artifact-uri.txt', emit: artifacts
    path 'native-task-evidence.json', emit: evidence
    script:
    def payload = maxwellPayload(meta, task, [recordings: recordings])
    """
    PYTHONPATH="\$PWD/${runtime}:\$PWD/${common}:/opt/ephys-modular/maxwell/scripts" python "\$PWD/${runtime}/native_task.py" raw <<'MAXWELL_REQUEST'
    ${payload}
    MAXWELL_REQUEST
    """
}

process MAXWELL_PREPARE {
    tag { meta.context.step_id }
    container { meta.owner.image }
    cpus 1
    memory '16 GB'
    disk '5 GB'
    time '4h'
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    input:
    tuple val(meta), val(artifacts)
    path runtime
    path common
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    tuple val(meta), path('conversion-plan.json'), emit: plan
    path 'native-task-evidence.json', emit: evidence
    script:
    def payload = maxwellPayload(meta, task, [artifacts: artifacts])
    """
    PYTHONPATH="\$PWD/${runtime}:\$PWD/${common}:/opt/ephys-modular/maxwell/scripts" python "\$PWD/${runtime}/native_task.py" prepare <<'MAXWELL_REQUEST'
    ${payload}
    MAXWELL_REQUEST
    """
}

process MAXWELL_CONVERT_UNIT {
    tag { "${meta.context.step_id}/${meta.unit_id}" }
    container { meta.owner.image }
    cpus { meta.cpus }
    memory '16 GB'
    // Exact binary GiB from the measured allocation, not compressed source size.
    disk { meta.disk_bytes as long }
    // Fused sequential phases retain the sum of all original phase time budgets.
    time '76h 15m'
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    input:
    tuple val(meta), path(plan)
    path runtime
    path common
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    path "outcome-${meta.unit_id}.json", emit: outcome
    path 'native-task-evidence.json', emit: evidence
    script:
    def payload = maxwellPayload(meta, task)
    """
    PYTHONPATH="\$PWD/${runtime}:\$PWD/${common}:/opt/ephys-modular/maxwell/scripts" python "\$PWD/${runtime}/native_task.py" unit --plan-file '${plan}' <<'MAXWELL_REQUEST'
    ${payload}
    MAXWELL_REQUEST
    """
}

process MAXWELL_COMMIT {
    tag { meta.context.step_id }
    container { meta.owner.image }
    cpus 1
    memory '16 GB'
    disk '5 GB'
    time '4h'
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    publishDir { "${launchDir}/task-reports/${meta.context.step_id}" }, mode: 'copy'
    ext {
        def stage_description = 'Verify every Maxwell unit and commit the complete NWB artifact envelope.'
        def artifact_outputs = '[{"id":"artifacts","label":"Verified NWB artifact envelope","location":{"type":"report_key","file":"result.txt","key":"artifacts_uri"}}]'
        return [stage_description: stage_description, artifact_outputs: artifact_outputs]
    }
    input:
    tuple val(meta), path(plan)
    path outcomes
    path runtime
    path common
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    path 'artifact-uri.txt', emit: artifacts
    path 'result.txt', emit: report
    path 'native-task-evidence.json', emit: evidence
    script:
    def payload = maxwellPayload(meta, task)
    def outcomeArgs = outcomes.collect { "--outcome-file '${it}'" }.join(' ')
    """
    PYTHONPATH="\$PWD/${runtime}:\$PWD/${common}:/opt/ephys-modular/maxwell/scripts" python "\$PWD/${runtime}/native_task.py" commit --plan-file '${plan}' ${outcomeArgs} <<'MAXWELL_REQUEST'
    ${payload}
    MAXWELL_REQUEST
    """
}

workflow PREPARE_MAXWELL_SELECTION {
    take:
    recordings
    settings
    context
    main:
    def image = new groovy.json.JsonSlurper().parse(file("${moduleDir}/image.json").toFile()).pinned_reference
    def meta = maxwellMetadata(settings, context, image)
    def bindings = (context.local_bind_paths ?: []).collect { file(it, checkIfExists: true) }
    MAXWELL_RAW_SELECTION(recordings.toList().map { values ->
        if (values.size() != 1) error 'Expected one complete recordings selection'
        tuple(meta, values[0])
    }, file("${moduleDir}/runtime"), file("${moduleDir}/components/common/python"), bindings)
    emit:
    artifacts = MAXWELL_RAW_SELECTION.out.artifacts.map { it.text.trim() }
}

workflow MAXWELL_TO_NWB {
    take:
    artifacts
    settings
    context
    main:
    def image = new groovy.json.JsonSlurper().parse(file("${moduleDir}/image.json").toFile()).pinned_reference
    def meta = maxwellMetadata(settings, context, image)
    def runtime = file("${moduleDir}/runtime")
    def common = file("${moduleDir}/components/common/python")
    def bindings = (context.local_bind_paths ?: []).collect { file(it, checkIfExists: true) }
    def policy = new groovy.json.JsonSlurper().parse(file("${moduleDir}/process-policy.json").toFile())
    def cpus = policy.processes.CONVERT_MAXONE_TO_NWB.cpus == 'available_processors' ? Runtime.runtime.availableProcessors() : policy.processes.CONVERT_MAXONE_TO_NWB.cpus
    MAXWELL_PREPARE(artifacts.toList().map { values ->
        if (values.size() != 1) error 'Expected one complete artifact envelope'
        tuple(meta, values[0])
    }, runtime, common, bindings)
    def tasks = MAXWELL_PREPARE.out.plan.flatMap { m, p ->
        def plan = new groovy.json.JsonSlurper().parse(p.toFile())
        plan.tasks.collect { unit -> tuple(m + [unit_id: unit.unit.unit_id,
            disk_bytes: unit.allocation.requested_bytes, cpus: cpus], p) }
    }
    MAXWELL_CONVERT_UNIT(tasks, runtime, common, bindings)
    MAXWELL_COMMIT(MAXWELL_PREPARE.out.plan, MAXWELL_CONVERT_UNIT.out.outcome.collect(), runtime, common, bindings)
    emit:
    artifacts = MAXWELL_COMMIT.out.artifacts.map { it.text.trim() }
}
