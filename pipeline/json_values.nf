nextflow.enable.dsl = 2

// Both supported Nextflow releases bundle Jackson. Keep this generic reader
// independent of the converter's Nextflow26/library/deployment requirements.
def meaJson(String text) {
    def mapper = new com.fasterxml.jackson.databind.ObjectMapper()
    mapper.enable(com.fasterxml.jackson.databind.DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
    mapper.enable(com.fasterxml.jackson.core.JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
    def value = mapper.readValue(text, Object.class)
    meaFiniteJson(value)
    value
}

def meaFiniteJson(value) {
    if (value instanceof Map) value.values().each { meaFiniteJson(it) }
    else if (value instanceof List) value.each { meaFiniteJson(it) }
    else if (value instanceof Double && !Double.isFinite(value))
        error 'MEA JSON floating values must be finite binary64 numbers.'
}

def meaReadJson(path) { meaJson(path.getText('UTF-8')) }

def meaCopyJson(value) {
    if (value instanceof Map) {
        def result = new LinkedHashMap()
        value.each { key,item -> result[key] = meaCopyJson(item) }
        return result
    }
    if (value instanceof List) return value.collect { meaCopyJson(it) }
    if (value instanceof GString) return value.toString()
    value
}

def meaSnapshot(value) {
    if (value == null) return null
    synchronized (value) {
        meaFiniteJson(value)
        groovy.json.JsonOutput.toJson(value)
        meaCopyJson(value)
    }
}
