nextflow.enable.dsl = 2

include { meaCodeDigest; meaCodeInput } from '../../pipeline/code_identity'

workflow {
    def request = new groovy.json.JsonSlurper().parseText(file(params.request).text)
    def results = request.collect { row ->
        try {
            def first = file(row.inputs[0])
            def port = meaCodeInput(first)
            assert port.size() == 2 && port[1] == first
            assert port[0] == meaCodeDigest([first])
            [name:row.name, digest:meaCodeDigest(row.inputs.collect { file(it) }),
             port_digest:port[0], port_path:port[1].toString()]
        } catch (Exception error) {
            def causes = []
            for (def cause = error; cause != null; cause = cause.cause)
                causes.add([error:cause.class.name, message:cause.message])
            [name:row.name, error:causes.last().error, message:causes.last().message, causes:causes]
        }
    }
    file(params.output).text = groovy.json.JsonOutput.toJson(results)
}
