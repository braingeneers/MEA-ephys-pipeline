/** File adapter transport only; all science, proofs and barriers remain Python-owned. */
class MaxwellNativeTask {
    static String quote(value) { "'" + value.toString().replace("'", "'\"'\"'") + "'" }

    static String command(job, task, runtime, common, String operation, Map options = [:], Map input = [:]) {
        def arguments = options.collectMany { key, value ->
            (value instanceof Collection ? value : [value]).collect { "--${key} ${quote(it)}" }
        }.join(' ')
        def payload = groovy.json.JsonOutput.toJson(input + [meta: job.meta, attempt: task.attempt,
            available_cpus: job.available_cpus, helper_bytes: job.helper_bytes,
            resources: [cpus: task.cpus, memory_bytes: task.memory.toBytes(),
                disk_bytes: task.disk.toBytes(), time_millis: task.time.toMillis()]])
        return """
        MAXWELL_OWNER_METADATA_ROOT="\$PWD" MAXWELL_POLICY_ROOT="\$PWD/${runtime}" \\
        PYTHONPATH="/opt/ephys-modular/maxwell-worker/runtime:\$PWD/${common}:/opt/ephys-modular/maxwell/scripts" \\
        python /opt/ephys-modular/maxwell-worker/runtime/native_invocation_task.py ${operation} ${arguments} <<'MAXWELL_REQUEST'
        ${payload}
        MAXWELL_REQUEST
        """
    }
}
