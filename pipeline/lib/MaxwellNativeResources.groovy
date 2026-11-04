/** Integer-only native_resources bridge using the same owning policy files.
 * Unit dispatch carries Python-derived complete budgets. Metadata budgets exist
 * before task scheduling; Docker tests compare both language implementations.
 */
class MaxwellNativeResources {
    static final BigInteger GIB = 1024G * 1024G * 1024G

    static BigInteger number(value, String label) {
        if (!(value instanceof Integer || value instanceof Long || value instanceof BigInteger) || value < 0)
            throw new IllegalArgumentException("${label} must be a nonnegative integer")
        return new BigInteger(value.toString())
    }

    static BigInteger ceilDiv(BigInteger value, BigInteger divisor) { (value + divisor - 1G).intdiv(divisor) }

    static Map metadata(String transport, Map nativePolicy, Map adoption, Map process,
                        long stagedBytes, long helperBytes, long minimumDiskGiB = 0L) {
        if (!(transport in ['local','s3']) || nativePolicy.schema_version != 1 ||
            nativePolicy.json_document_limit_bytes != 33554432 || stagedBytes < 0 || helperBytes < 0 || minimumDiskGiB < 0)
            throw new IllegalArgumentException('Invalid native metadata resource input')
        def jsonBytes = number(nativePolicy.json_document_limit_bytes,'JSON bytes')
        def reserve = number(process.minimum_free_gb,'Reserve') * GIB
        def state = number(adoption.phase_logs_and_checks_allowance_bytes,'Protocol state')
        def caches = number(adoption.private_runtime_cache_allowance_bytes,'Protocol caches')
        def headroom = 100G + number(adoption.workspace_headroom_percent,'Headroom')
        def innerFiles = state + caches + (transport == 'local' ? 6G * jsonBytes : 0G)
        def innerRequired = ceilDiv(innerFiles * headroom,100G) + reserve
        def innerRequested = ceilDiv(innerRequired,GIB) * GIB
        def simultaneous = innerRequested - reserve + caches + number(nativePolicy.native_cache_allowance_bytes,'Native caches') +
            number(nativePolicy.native_json_slots,'Native slots') * jsonBytes + number(nativePolicy.native_log_allowance_bytes,'Native logs') +
            number(nativePolicy.staged_copy_multiplier,'Staged copies') * (number(stagedBytes,'Metadata bytes') + number(helperBytes,'Helper bytes'))
        def required = ceilDiv(simultaneous * headroom,100G) + reserve
        def requested = (ceilDiv(required,GIB) * GIB).max(number(minimumDiskGiB,'Minimum disk') * GIB)
        return [required_bytes: required.longValueExact(), requested_bytes: requested.longValueExact(), minimum_free_bytes: reserve.longValueExact()]
    }

    static Map processResources(String operation, Map nativePolicy, Map process, String action = null, long availableCpus = 1L) {
        def role = operation == 'unit' ? action : operation
        if (!nativePolicy.roles.containsKey(role)) throw new IllegalArgumentException('Unknown native resource role')
        def result = new LinkedHashMap(nativePolicy.roles[role])
        if (result.cpus == 'available_processors') {
            if (availableCpus < 1) throw new IllegalArgumentException('Available processors must be positive')
            result.cpus = availableCpus
        }
        if (result.time_millis == 'scientific_phase_sum')
            result.time_millis = process.processes.values().collect { new nextflow.util.Duration(it.time).toMillis() }.sum()
        result.max_forks = nativePolicy.max_forks
        result.max_retries = nativePolicy.max_retries
        return result
    }

    static long helperBytes(java.nio.file.Path runtime, java.nio.file.Path commonPython) {
        long total = 0L
        for (root in [runtime, commonPython.resolve('ephys_modular')]) {
            java.nio.file.Files.walk(root).withCloseable { stream ->
                stream.filter { path -> java.nio.file.Files.isRegularFile(path) &&
                    (path.fileName.toString().endsWith('.py') || path.fileName.toString().endsWith('.json')) &&
                    (root != runtime || path.parent == root) }.forEach { path -> total += java.nio.file.Files.size(path) }
            }
        }
        for (name in ['nextflow.config','image.json','process-policy.json'])
            total += java.nio.file.Files.size(runtime.parent.resolve(name))
        return total
    }
}
