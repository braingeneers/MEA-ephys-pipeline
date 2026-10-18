/** Owner policy: retry proven pre-command failures, never unknown science. */
class MaxwellTaskRetry {
    static Integer wrapperExit(workDir) {
        if (workDir == null) return null
        try {
            def value = java.nio.file.Files.readString(workDir.resolve('.exitcode')).trim()
            return value ==~ /-?\d+/ ? value as Integer : Integer.MAX_VALUE
        }
        catch (java.nio.file.NoSuchFileException ignored) { return null }
        catch (Exception ignored) { return Integer.MAX_VALUE }
    }

    static Map decision(exitStatus, Integer wrapper, Throwable failure, int attempt) {
        def unknownExit = exitStatus == null || exitStatus == Integer.MAX_VALUE
        def noWrapperFailure = wrapper == null || wrapper == 0
        def preCommand = false
        if (noWrapperFailure) {
            def current = failure
            for (int depth = 0; current != null && depth < 8; depth++) {
                if (unknownExit && (current.class.name == 'nextflow.k8s.client.PodUnschedulableException' ||
                    (current.class.name == 'nextflow.exception.ProcessFailedException' &&
                     current.message ==~ /K8s pod 'nf-[0-9a-f]{32}-[0-9a-f]{5}-[a-z0-9]{5}' execution failed - reason: TaintToleration - message: Pod was rejected: Predicate TaintToleration failed: node\(s\) had taints that the pod didn't tolerate/))) {
                    preCommand = true
                }
                current = current.cause
            }
            // OCI runtime-create exit; a genuine command writes its nonzero exit.
            if (exitStatus == 128) preCommand = true
        }
        def retry = preCommand && attempt >= 1 && attempt <= 2
        return [action: retry ? 'retry' : 'terminate', pre_command: preCommand,
            delay_millis: retry ? 15_000L * (1L << (attempt - 1)) : 0L]
    }

    static String strategy(task) {
        def result = decision(task.exitStatus, wrapperExit(task.workDir), task.previousException, task.attempt as int)
        org.slf4j.LoggerFactory.getLogger('MaxwellTaskRetry').warn(
            "Maxwell task ${task.process}: pre_command=${result.pre_command}; attempt=${task.attempt}; action=${result.action}; delay_seconds=${result.delay_millis.intdiv(1000)}")
        if (result.delay_millis) Thread.sleep(result.delay_millis as long)
        return result.action
    }
}
