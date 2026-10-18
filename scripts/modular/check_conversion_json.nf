// Compare file/string/typed/channel results to original input bytes in Python.
include { conversionJson; conversionValue; conversionSnapshot } from '../../pipeline/conversion'

workflow {
    def text = file(params.input_file,checkIfExists:true).getText('UTF-8')
    def fromFile = conversionValue([:],params.input_file,'settings',true)
    def fromString = conversionValue(text,null,'settings',true)
    def fromTyped = conversionValue(fromFile,null,'settings',true)
    def cases = [file:fromFile,string:fromString,typed:conversionSnapshot(fromTyped)]
    def arrayText = '['+text+']'
    cases.array = conversionValue(arrayText,null,'recordings',false)[0]
    def invalid = [
        [text,params.input_file,'settings',true],
        ['[]',null,'settings',true], ['{}',null,'recordings',false],
        ['[]',null,'recordings',false], ['null',null,'settings',true],
        ['true',null,'settings',true], ['1',null,'settings',true],
        ['{"x":NaN}',null,'settings',true], ['{"x":1e9999}',null,'settings',true],
        ['{"x":1,"x":2}',null,'settings',true], ['{} {}',null,'settings',true]
    ]
    invalid.each { arguments ->
        boolean rejected = false
        try { conversionValue(*arguments) } catch (Exception ignored) { rejected = true }
        assert rejected : "Accepted invalid conversion boundary: ${arguments}"
    }
    def pool = java.util.concurrent.Executors.newFixedThreadPool(2)
    try {
        for (int iteration=0; iteration<40; iteration++) {
            def shared = conversionJson(text)
            def futures = (0..<2).collect { alias ->
                pool.submit({ -> conversionSnapshot(shared) } as java.util.concurrent.Callable)
            }
            def copies = futures.collect { it.get() }
            assert copies.every { groovy.json.JsonOutput.toJson(it) == groovy.json.JsonOutput.toJson(fromFile) }
            copies[0].unknown.nested.add('private')
            assert !copies[1].unknown.nested.contains('private')
            assert !shared.unknown.nested.contains('private')
        }
    } finally { pool.shutdownNow() }
    channel.value(fromFile).map { value ->
        cases.channel = conversionSnapshot(value)
        file('decoded-cases.json').text = groovy.json.JsonOutput.toJson(cases)
        'CONVERSION_JSON_OK: file/string/typed/channel, 40 concurrent pairs, 11 invalid inputs rejected'
    }.view()
}
