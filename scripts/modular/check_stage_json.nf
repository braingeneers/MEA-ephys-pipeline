// Metadata plumbing proof, never a scientific producer substitute.
include { meaJson; meaReadJson; meaSnapshot } from '../../pipeline/json_values'

workflow {
    def text = file(params.input_file,checkIfExists:true).getText('UTF-8')
    def original = meaJson(text)
    def cases = [file:meaReadJson(file(params.input_file)),string:meaJson(text),
                 typed:meaSnapshot(original),nullSnapshot:meaSnapshot(null)]
    def invalid = ['{"x":NaN}','{"x":Infinity}','{"x":-Infinity}',
        '{"x":1e9999}','{"x":-1e9999}','{"x":1,"x":2}','{"nested":{"x":1,"x":2}}',
        '{} {}','[1,]','{"x":}','{"x":01}','{"x":undefined}']
    invalid.each { candidate ->
        boolean rejected = false
        try { meaJson(candidate) } catch (Exception ignored) { rejected = true }
        assert rejected : "Accepted invalid stage JSON: ${candidate}"
    }
    def pool = java.util.concurrent.Executors.newFixedThreadPool(2)
    try {
        for (int iteration=0; iteration<40; iteration++) {
            def shared = meaJson(text)
            def futures = (0..<2).collect { alias ->
                pool.submit({ -> meaSnapshot(shared) } as java.util.concurrent.Callable)
            }
            def copies = futures.collect { it.get() }
            assert copies.every { groovy.json.JsonOutput.toJson(it) == groovy.json.JsonOutput.toJson(original) }
            copies[0].unknown.nested.add('private')
            assert !copies[1].unknown.nested.contains('private')
            assert !shared.unknown.nested.contains('private')
        }
    } finally { pool.shutdownNow() }
    channel.value(original).map { value ->
        cases.channel = meaSnapshot(value)
        file('decoded-stage-json.json').text = groovy.json.JsonOutput.toJson(cases)
        'STAGE_JSON_OK: original numbers, independent snapshots, 40 concurrent pairs, 12 invalid inputs rejected'
    }.view()
}
