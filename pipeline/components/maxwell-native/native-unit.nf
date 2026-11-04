nextflow.enable.dsl = 2

// Imported under three process aliases: one maintained selected-unit implementation.
process MAXWELL_UNIT {
    tag { "${job.meta.context.step_id}/${job.unit_id}" }
    container { job.meta.owner.image }
    cpus { job.resources.cpus }
    memory { job.resources.memory_bytes as long }
    disk { job.resources.disk_bytes as long }
    time { new nextflow.util.Duration(job.resources.time_millis as long) }
    maxForks 4
    maxRetries 2
    errorStrategy { MaxwellTaskRetry.strategy(task) }
    input:
    tuple val(job), path(plan), path(plan_digest)
    path runtime
    path common
    path owner_files
    path local_bindings, stageAs: 'local-bindings??/*'
    output:
    tuple val(job), path(plan), path(plan_digest),
        path("native-task/outputs/outcome-${job.unit_id}.json"),
        path("native-task/outputs/binding-${job.unit_id}.json"),
        path("native-task/outputs/binding-digest-${job.unit_id}.txt"), emit: completed
    path 'native-task/outputs/native-task-evidence.json', emit: evidence
    script:
    MaxwellNativeTask.command(job, task, runtime, common, 'unit', [
        'plan-file': plan, 'expected-plan-digest-file': plan_digest, 'unit-id': job.unit_id,
        action: job.action, 'scheduled-plan-digest': job.expected_plan_digest])
}
