nextflow.enable.dsl = 2
include { MAXWELL_UNIT as MAXWELL_FRESH; MAXWELL_UNIT as MAXWELL_ADOPT; MAXWELL_UNIT as MAXWELL_REUSE } from './native-unit'

// Jackson retains JSON integer/floating encodings and IEEE signed zero.
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

// Per-invocation scientific settings and intent; global params are deployment only.
def maxwellMetadata(settings, context, image, allowRecordingStates = false) {
    if (!(settings instanceof Map)) error 'Maxwell settings must be an object'
    def submitted = new LinkedHashMap(settings)
    def controls = context.containsKey('controls') ? context.controls : [:]
    if (submitted.containsKey('settings')) {
        if (submitted.keySet().any { !['settings', 'controls'].contains(it) }) error 'Unknown Maxwell parameter wrapper field'
        if (!(submitted.settings instanceof Map)) error 'The settings parameter must be an object'
        if (submitted.containsKey('controls')) {
            if (context.containsKey('controls')) error 'Supply controls through the parameter wrapper or context, not both'
            controls = submitted.controls
        }
        submitted = new LinkedHashMap(submitted.settings)
    }
    if (!(controls instanceof Map)) error 'Maxwell controls must be an object'
    if (!allowRecordingStates && controls.containsKey('recording_source_states'))
        error 'recording_source_states requires PREPARE_MAXWELL_SELECTION; use the standalone recordings parameter'
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
        settings: submitted, transport: root.startsWith('s3://') ? 's3' : 'local',
        controls: new LinkedHashMap(controls)]
}

def maxwellEnvironment(base, queueSize) {
    def runtime = base.resolve('runtime')
    def common = base.resolve('components/common/python')
    def rules = maxwellJson(runtime.resolve('native-resource-policy.json').text)
    // Distinct action processors share the executor queue. Do not multiply the
    // original four-task ceiling when a caller tunes the deployment queue.
    if (!(queueSize instanceof Number) || queueSize < 1 || queueSize > rules.max_forks ||
        queueSize.toString() != queueSize.intValue().toString())
        error "Maxwell native task_queue_size must be an integer from 1 to ${rules.max_forks}"
    // Resolve executor-specific and final admission overrides just as Nextflow does.
    def config = nextflow.Global.config
    def executor = (config.process.executor ?: 'local').toString()
    def effectiveQueue = new nextflow.executor.ExecutorConfig(config.executor ?: [:]).getQueueSize(executor, 0)
    if (effectiveQueue < 1 || effectiveQueue > queueSize)
        error "Maxwell native effective ${executor} queue (${effectiveQueue}) exceeds task_queue_size (${queueSize}) or is unlimited; review the final launch/admission config"
    if (rules.max_forks != 4 || rules.max_retries != 2) error 'Update native process concurrency/retry directives with their policy'
    return [runtime: runtime, common: common, rules: rules,
        adoption: maxwellJson(runtime.resolve('adoption-allocation-policy.json').text),
        process: maxwellJson(base.resolve('process-policy.json').text),
        helper_bytes: MaxwellNativeResources.helperBytes(runtime, common),
        available_cpus: Runtime.runtime.availableProcessors(),
        owner_files: ['nextflow.config', 'image.json', 'process-policy.json'].collect { base.resolve(it) }]
}

def maxwellJob(meta, env, operation, files = []) {
    def resources = MaxwellNativeResources.processResources(operation, env.rules, env.process, null, env.available_cpus as long)
    resources.disk_bytes = MaxwellNativeResources.metadata(meta.transport, env.rules, env.adoption, env.process,
        files.collect { it.size() as long }.sum(0L) as long, env.helper_bytes as long,
        (meta.runtime.minimum_disk_gib ?: 0) as long).requested_bytes
    return [meta: meta, available_cpus: env.available_cpus, helper_bytes: env.helper_bytes, resources: resources]
}

def maxwellDispatch(job, plan, digest, dispatch) {
    def fields = ['unit_id','action','expected_plan_digest','disk_bytes','cpus','memory_bytes','time_millis','max_forks','max_retries']
    if (dispatch.size() > 33554432 || digest.size() != 65) error 'Invalid bounded native dispatch/digest files'
    def expected = digest.text.trim()
    if (!(expected ==~ /[a-f0-9]{64}/)) error 'Invalid retained plan digest'
    def lines = dispatch.readLines()
    if (!lines || lines[0].split('\t', -1).toList() != fields) error 'Invalid native dispatch columns'
    def ids = new HashSet()
    def rows = lines.drop(1)
    if (!rows) error 'Native dispatch must contain selected units'
    return rows.withIndex().collect { line, ordinal ->
        def values = line.split('\t', -1).toList()
        if (values.size() != fields.size()) error 'Invalid closed native dispatch row'
        def row = [fields, values].transpose().collectEntries { it }
        if (!ids.add(row.unit_id) || !(row.action in ['fresh','adopt','reuse']) || row.expected_plan_digest != expected)
            error 'Duplicate, foreign or unknown native dispatch'
        def resources = fields.drop(3).collectEntries { key ->
            if (!(row[key] ==~ /[1-9][0-9]*/)) error 'Invalid integer native dispatch resource'
            [(key): new BigInteger(row[key]).longValueExact()]
        }
        if (resources.max_forks != 4 || resources.max_retries != 2) error 'Native dispatch policy differs'
        tuple(job + [unit_id: row.unit_id, action: row.action, expected_plan_digest: expected,
            ordinal: ordinal, selected_count: rows.size(), resources: resources], plan, digest)
    }
}

process MAXWELL_RAW_SELECTION {
    tag { job.meta.context.step_id }
    container { job.meta.owner.image }
    cpus { job.resources.cpus }
    memory { job.resources.memory_bytes as long }
    disk { job.resources.disk_bytes as long }
    time { new nextflow.util.Duration(job.resources.time_millis as long) }
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    input:
    tuple val(job), val(recordings)
    path runtime
    path common
    path owner_files
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    path 'native-task/outputs/artifact-uri.txt', emit: artifacts
    path 'native-task/outputs/native-task-evidence.json', emit: evidence
    script:
    MaxwellNativeTask.command(job, task, runtime, common, 'raw', [:], [recordings: recordings])
}

process MAXWELL_DISCOVER {
    tag { job.meta.context.step_id }
    container { job.meta.owner.image }
    cpus { job.resources.cpus }
    memory { job.resources.memory_bytes as long }
    disk { job.resources.disk_bytes as long }
    time { new nextflow.util.Duration(job.resources.time_millis as long) }
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    input:
    tuple val(job), val(artifacts)
    path runtime
    path common
    path owner_files
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    tuple val(job), path('native-task/outputs/discovery.json'), path('native-task/outputs/discovery-digest.txt'), emit: discovery
    path 'native-task/outputs/native-task-evidence.json', emit: evidence
    script:
    MaxwellNativeTask.command(job, task, runtime, common, 'discover', [:], [artifacts: artifacts])
}

process MAXWELL_PREPARE {
    tag { job.meta.context.step_id }
    container { job.meta.owner.image }
    cpus { job.resources.cpus }
    memory { job.resources.memory_bytes as long }
    disk { job.resources.disk_bytes as long }
    time { new nextflow.util.Duration(job.resources.time_millis as long) }
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    input:
    tuple val(job), path(discovery), path(discovery_digest)
    path runtime
    path common
    path owner_files
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    tuple val(job), path('native-task/outputs/invocation-plan.json'), path('native-task/outputs/plan-digest.txt'),
        path('native-task/outputs/dispatch.tsv'), emit: plan
    path 'native-task/outputs/native-task-evidence.json', emit: evidence
    script:
    MaxwellNativeTask.command(job, task, runtime, common, 'prepare', [
        'discovery-file': discovery, 'expected-discovery-digest-file': discovery_digest])
}

process MAXWELL_FREEZE_BINDINGS {
    tag { job.meta.context.step_id }
    container { job.meta.owner.image }
    cpus { job.resources.cpus }
    memory { job.resources.memory_bytes as long }
    disk { job.resources.disk_bytes as long }
    time { new nextflow.util.Duration(job.resources.time_millis as long) }
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    input:
    tuple val(job), path(plan), path(plan_digest), path(bindings), path(binding_digests)
    path runtime
    path common
    path owner_files
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    tuple path('native-task/outputs/bindings.json'), path('native-task/outputs/bindings-digest.txt'), emit: bindings
    path 'native-task/outputs/native-task-evidence.json', emit: evidence
    script:
    MaxwellNativeTask.command(job, task, runtime, common, 'freeze-bindings', [
        'plan-file': plan, 'expected-plan-digest-file': plan_digest,
        'unit-binding-file': bindings, 'unit-binding-digest-file': binding_digests])
}

process MAXWELL_COMMIT {
    tag { job.meta.context.step_id }
    container { job.meta.owner.image }
    cpus { job.resources.cpus }
    memory { job.resources.memory_bytes as long }
    disk { job.resources.disk_bytes as long }
    time { new nextflow.util.Duration(job.resources.time_millis as long) }
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    publishDir { "${launchDir}/task-reports/${job.meta.context.step_id}" }, mode: 'copy', saveAs: { it.tokenize('/').last() }
    ext {
        def stage_description = 'Verify every Maxwell unit and commit the complete NWB artifact envelope.'
        def artifact_outputs = '[{"id":"artifacts","label":"Verified NWB artifact envelope","location":{"type":"report_key","file":"result.txt","key":"artifacts_uri"}}]'
        return [stage_description: stage_description, artifact_outputs: artifact_outputs]
    }
    input:
    tuple val(job), path(plan), path(plan_digest), path(outcomes), path(bindings), path(bindings_digest)
    path runtime
    path common
    path owner_files
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    path 'native-task/outputs/artifact-uri.txt', emit: artifacts
    path 'native-task/outputs/result.txt', emit: report
    path 'native-task/outputs/native-task-evidence.json', emit: evidence
    script:
    MaxwellNativeTask.command(job, task, runtime, common, 'commit', [
        'plan-file': plan, 'expected-plan-digest-file': plan_digest, 'outcome-file': outcomes,
        'bindings-file': bindings, 'expected-bindings-digest-file': bindings_digest])
}

workflow PREPARE_MAXWELL_SELECTION {
    take:
    recordings
    settings
    context
    main:
    def base = file(moduleDir)
    def env = maxwellEnvironment(base, params.task_queue_size)
    def meta = maxwellMetadata(settings, context, maxwellJson(base.resolve('image.json').text).pinned_reference, true)
    def local = (context.local_bind_paths ?: []).collect { file(it, checkIfExists: true) }
    MAXWELL_RAW_SELECTION(recordings.toList().filter { !it.isEmpty() }.map { values ->
        if (values.size() != 1) error 'Expected one complete recordings selection'
        tuple(maxwellJob(meta, env, 'raw'), values[0])
    }, env.runtime, env.common, env.owner_files, local)
    emit:
    artifacts = MAXWELL_RAW_SELECTION.out.artifacts.map { it.text.trim() }
}

workflow MAXWELL_TO_NWB {
    take:
    artifacts
    settings
    context
    main:
    def base = file(moduleDir)
    def env = maxwellEnvironment(base, params.task_queue_size)
    def meta = maxwellMetadata(settings, context, maxwellJson(base.resolve('image.json').text).pinned_reference)
    def local = (context.local_bind_paths ?: []).collect { file(it, checkIfExists: true) }
    MAXWELL_DISCOVER(artifacts.toList().filter { !it.isEmpty() }.map { values ->
        if (values.size() != 1) error 'Expected one complete artifact envelope'
        tuple(maxwellJob(meta, env, 'discover'), values[0])
    }, env.runtime, env.common, env.owner_files, local)
    MAXWELL_PREPARE(MAXWELL_DISCOVER.out.discovery.map { job, d, digest ->
        tuple(maxwellJob(meta, env, 'prepare', [d, digest]), d, digest)
    }, env.runtime, env.common, env.owner_files, local)
    def dispatch = MAXWELL_PREPARE.out.plan.flatMap { job, plan, digest, rows -> maxwellDispatch(job, plan, digest, rows) }
    def actions = dispatch.branch { job, plan, digest ->
        fresh: job.action == 'fresh'
        adopt: job.action == 'adopt'
        reuse: job.action == 'reuse'
    }
    MAXWELL_FRESH(actions.fresh, env.runtime, env.common, env.owner_files, local)
    MAXWELL_ADOPT(actions.adopt, env.runtime, env.common, env.owner_files, local)
    MAXWELL_REUSE(actions.reuse, env.runtime, env.common, env.owner_files, local)
    def completed = MAXWELL_FRESH.out.completed.mix(MAXWELL_ADOPT.out.completed, MAXWELL_REUSE.out.completed)
        .collect(flat: false).filter { !it.isEmpty() }.map { rows ->
            def ordered = rows.sort { it[0].ordinal }
            if (!ordered || ordered.size() != ordered[0][0].selected_count ||
                ordered.collect { it[0].unit_id }.unique().size() != ordered.size()) error 'Incomplete native selected-unit fan-in'
            tuple(ordered[0][1], ordered[0][2], ordered.collect { it[3] }, ordered.collect { it[4] }, ordered.collect { it[5] })
        }
    MAXWELL_FREEZE_BINDINGS(completed.map { plan, digest, outcomes, bindings, digests ->
        tuple(maxwellJob(meta, env, 'freeze-bindings', [plan, digest] + bindings + digests), plan, digest, bindings, digests)
    }, env.runtime, env.common, env.owner_files, local)
    MAXWELL_COMMIT(completed.combine(MAXWELL_FREEZE_BINDINGS.out.bindings).map { plan, digest, outcomes, unitBindings, unitDigests, bindings, bindingDigest ->
        tuple(maxwellJob(meta, env, 'commit', [plan, digest, bindings, bindingDigest] + outcomes),
            plan, digest, outcomes, bindings, bindingDigest)
    }, env.runtime, env.common, env.owner_files, local)
    emit:
    artifacts = MAXWELL_COMMIT.out.artifacts.map { it.text.trim() }
}
