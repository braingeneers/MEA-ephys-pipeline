// Concurrent JSON-boundary regression, not scientific conversion evidence.
include { conversionSnapshot } from '../../pipeline/conversion'

workflow {
    def pool = java.util.concurrent.Executors.newFixedThreadPool(2)
    try {
        for (int run=0; run<40; run++) {
            def text = groovy.json.JsonOutput.toJson((0..<100).collect { index ->
                [input_uri:"file:///data/${index}/same.raw.h5",unknown:[index:index,
                    nested:[keep:[true,null,false,3,2.5],integer:7,floating:7.0]]]
            })
            def shared = new groovy.json.JsonSlurper().parseText(text)
            def expected = new groovy.json.JsonSlurperClassic().parseText(text)
            def ready = new java.util.concurrent.CountDownLatch(2)
            def start = new java.util.concurrent.CountDownLatch(1)
            def futures = (0..<2).collect { alias ->
                pool.submit({ ->
                    ready.countDown()
                    start.await()
                    conversionSnapshot(shared)
                } as java.util.concurrent.Callable)
            }
            ready.await()
            start.countDown()
            def copies = futures.collect { it.get() }
            assert copies.every { it == expected }
            assert copies.every { groovy.json.JsonOutput.toJson(it).contains('"floating":7.0') }
            copies[0][0].unknown.nested.keep.add('private-edit')
            assert copies[1] == expected
            assert new groovy.json.JsonSlurperClassic().parseText(groovy.json.JsonOutput.toJson(shared)) == expected
        }
        def ordered = new LinkedHashMap([second:-0.0d,first:7.0d,integer:7])
        def exact = conversionSnapshot(ordered)
        assert exact.keySet().toList() == ['second','first','integer']
        assert groovy.json.JsonOutput.toJson(exact) == '{"second":-0.0,"first":7.0,"integer":7}'
    } finally {
        pool.shutdownNow()
    }
    println 'CONVERSION_SNAPSHOT_OK: 40 concurrent pairs, complete typed metadata and independent copies'
}
