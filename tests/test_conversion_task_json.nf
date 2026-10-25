nextflow.enable.dsl = 2

include { conversionReadJson; conversionTaskJson } from '../pipeline/conversion'

params.fixture_json = ''
params.output_json = ''
params.temporary_dir = ''

class ObservedJsonFile {
    java.nio.file.Path path
    java.util.concurrent.CountDownLatch firstEmpty = new java.util.concurrent.CountDownLatch(1)
    int reads = 0
    int emptyReads = 0

    String getText(String charset) {
        reads++
        def text = new String(java.nio.file.Files.readAllBytes(path), charset)
        if (text.isEmpty()) {
            emptyReads++
            firstEmpty.countDown()
        }
        return text
    }

}

def failureOf(Closure operation) {
    Throwable failure = null
    try { operation.call() }
    catch (Throwable e) { failure = e }
    assert failure != null: 'An invalid input was accepted.'
    while (failure.cause != null) failure = failure.cause
    return failure
}

def delayedJson(path, String text) {
    path.text = ''
    def observed = new ObservedJsonFile(path:path)
    def threadFailure = new java.util.concurrent.atomic.AtomicReference<Throwable>()
    def writer = new Thread({
        try {
            assert observed.firstEmpty.await(3L, java.util.concurrent.TimeUnit.SECONDS)
            Thread.sleep(25L)
            def temporary = path.resolveSibling(path.fileName.toString()+'.ready')
            temporary.text = text
            java.nio.file.Files.move(temporary, path, java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        } catch (Throwable e) { threadFailure.set(e) }
    } as Runnable)
    writer.start()
    def value
    try { value = conversionTaskJson(observed) }
    finally { writer.join(5000L) }
    assert !writer.alive && threadFailure.get() == null
    assert observed.emptyReads >= 1 && observed.reads >= 2
    return [value:value, reads:observed.reads, empty_reads:observed.emptyReads]
}

// Control-data producer only. No native/scientific conversion is executed.
process EMIT_BRIDGE_METADATA {
    cpus 1
    memory '128 MB'
    input:
    path fixture
    output:
    path 'bridge-result.json', emit: result
    script:
    """
    cp '${fixture}' bridge-result.json
    """
}

workflow {
    def root = file(params.temporary_dir)
    assert !java.nio.file.Files.exists(root)
    java.nio.file.Files.createDirectories(root)
    def cases = []
    def originalText = file(params.fixture_json, checkIfExists:true).getText('UTF-8')
    def original = conversionReadJson(file(params.fixture_json))
    assert original.complete && original.units.size() == 6

    def immediate = root.resolve('immediate.json')
    immediate.text = originalText
    assert groovy.json.JsonOutput.toJson(conversionTaskJson(immediate)) == groovy.json.JsonOutput.toJson(original)
    cases.add('immediate_real_metadata')
    def recovered = delayedJson(root.resolve('delayed.json'), originalText)
    assert groovy.json.JsonOutput.toJson(recovered.value) == groovy.json.JsonOutput.toJson(original)
    cases.add('empty_then_complete_real_metadata')

    def numericText = '{"integer":1,"float":1.0,"zero":-0.0,"large":922337203685477580812345,"small":1e-300,"large_float":1e300,"unknown":{"nested":[true,null,"value"]}}'
    def numeric = delayedJson(root.resolve('numeric.json'), numericText).value
    assert numeric.integer instanceof Integer && numeric.integer == 1
    assert numeric.float instanceof Double && numeric.float == 1.0D
    assert numeric.zero instanceof Double && Double.doubleToRawLongBits(numeric.zero) == Long.MIN_VALUE
    assert numeric.large instanceof BigInteger && numeric.large.toString() == '922337203685477580812345'
    assert Double.doubleToRawLongBits(numeric.small) == Double.doubleToRawLongBits(Double.parseDouble('1e-300'))
    assert Double.doubleToRawLongBits(numeric.large_float) == Double.doubleToRawLongBits(Double.parseDouble('1e300'))
    assert numeric.unknown.nested == [true,null,'value']
    cases.add('original_numeric_types_bits_and_unknown_fields')

    def rejectionMessages = [malformed:'Unexpected character', whitespace:'No content to map due to end-of-input',
        duplicate:'Duplicate field', trailing:'Trailing token', nonfinite:'finite binary64']
    def rejections = [:]
    [malformed:'{broken', whitespace:' \n', duplicate:'{"x":1,"x":2}',
     trailing:'{} {}', nonfinite:'{"x":1e999}'].each { name, text ->
        def path = root.resolve(name+'.json')
        path.text = text
        def observed = new ObservedJsonFile(path:path)
        def error = failureOf { conversionTaskJson(observed) }
        assert observed.reads == 1 && observed.emptyReads == 0
        assert error.message.contains(rejectionMessages[name])
        rejections[name] = [exception:error.class.name, message:error.message, reads:observed.reads]
        cases.add('reject_'+name+'_without_retry')
    }

    def empty = root.resolve('permanently-empty.json')
    empty.text = ''
    def observed = new ObservedJsonFile(path:empty)
    long started = System.nanoTime()
    def error = failureOf { conversionTaskJson(observed, 250L) }
    long elapsedMillis = java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-started)
    assert error instanceof IOException && error.message.contains('stayed empty for 250 ms')
    assert observed.emptyReads >= 2 && elapsedMillis >= 250L && elapsedMillis < 5000L
    cases.add('permanent_empty_has_finite_failure')

    def missing = new ObservedJsonFile(path:root.resolve('missing.json'))
    error = failureOf { conversionTaskJson(missing) }
    assert error instanceof java.nio.file.NoSuchFileException && missing.reads == 1
    cases.add('missing_file_rejected_without_retry')
    def caller = new ObservedJsonFile(path:empty)
    error = failureOf { conversionReadJson(caller) }
    assert error.message.contains('No content to map due to end-of-input') && caller.reads == 1
    cases.add('ordinary_caller_json_remains_strict')
    [-1L,60001L].each { timeout ->
        error = failureOf { conversionTaskJson(caller, timeout) }
        assert error instanceof IllegalArgumentException && caller.reads == 1
        cases.add('reject_invalid_budget_'+timeout)
    }

    file(params.output_json).text = groovy.json.JsonOutput.toJson([
        passed:true,cases:cases,real_units:original.units.size(),
        recovered_read_count:recovered.reads,recovered_empty_reads:recovered.empty_reads,
        permanent_empty_elapsed_ms:elapsedMillis,strict_numeric_fidelity:true,rejections:rejections,
        scientific_execution:false,kernel_cause_reproduced:false])+'\n'
    println "CONVERSION_TASK_JSON_PASS=${cases.size()}"

    EMIT_BRIDGE_METADATA(file(params.fixture_json))
    EMIT_BRIDGE_METADATA.out.result.map { conversionTaskJson(it) }
        .flatMap { document -> document.units.collect { it.metadata } }
        .collect(flat:false).map { rows ->
            assert rows.size() == 6
            assert groovy.json.JsonOutput.toJson(rows) == groovy.json.JsonOutput.toJson(original.units.collect { it.metadata })
            file(params.output_json+'.ports.json').text = groovy.json.JsonOutput.toJson([
                passed:true,actual_task_output_to_value_port:true,units:rows.size(),
                metadata_exact:true,scientific_execution:false])+'\n'
            return true
        }.view()
}
