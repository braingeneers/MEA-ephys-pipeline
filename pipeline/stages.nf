nextflow.enable.dsl = 2

include { meaCodeInput } from './code_identity'

include { processOptions } from './settings'
include { meaReadJson; meaSnapshot } from './json_values'
include { job_dispatch } from './modules/job_dispatch'
include { preprocessing } from './modules/preprocessing'
include { spikesort_kilosort4 } from './modules/spikesort_kilosort4'
include { postprocessing } from './modules/postprocessing'
include { curation } from './modules/curation'
include { visualization } from './modules/visualization'
include { results_collector } from './modules/results_collector'
include { report_generation } from './modules/report_generation'
include { burst_detection } from './modules/burst_detection'
include { nwb_ecephys } from './modules/nwb_ecephys'
include { nwb_units } from './modules/nwb_units'
include { bundle_stage as PACK_DISPATCH; bundle_stage as PACK_PREPARED; bundle_stage as PACK_SORTED;
          bundle_stage as PACK_ANALYZED; bundle_stage as PACK_REPORTED; bundle_stage as PACK_EXPORTED } from './modules/bundle_stage'
include { verify_stage as VERIFY_PREPARED; verify_stage as VERIFY_SORTED;
          verify_stage as VERIFY_ANALYZED; verify_stage as VERIFY_REPORTED } from './modules/verify_stage'

def stageInputs(units) {
    units.toList().flatMap { rows ->
        if (!rows) error 'A scientific stage requires at least one unit.'
        def ids = []
        rows.each { meta, path ->
            if (!(meta instanceof Map) || !(meta.unit_id instanceof String) || !(meta.unit_id ==~ /[A-Za-z0-9][A-Za-z0-9_.-]{0,159}/))
                error 'Every unit needs a stable path-safe metadata.unit_id.'
            if (!path.toFile().isDirectory()) error "Unit input must be a POSIX directory: ${path}"
            ids.add(meta.unit_id)
        }
        if (ids.toSet().size() != ids.size()) error 'Duplicate unit identities in stage input.'
        rows.collect { meta,path -> tuple(meaSnapshot(meta),path) }
    }
}

def stageContext(Map context) {
    if (!context.output_uri || !context.workflow_version) error 'Stage context requires output_uri and owning workflow_version.'
    if (context.output_uri.toString().contains('://')) error 'Portable local stages require a POSIX output directory; S3 transfer belongs to its adapter.'
    // Retain only declared execution provenance, never copy shared runner params.
    [output_uri:new File(context.output_uri.toString()).absolutePath, workflow_version:context.workflow_version.toString(),
     run_id:context.get('run_id'), step_id:context.get('step_id')]
}

// Deployment estimates can arrive from an upstream preflight task. Keep them
// outside scientific unit metadata, and resolve one broadcast value per call.
def unitResources(String process, Map settings, Map context, Map meta) {
    def resources = new LinkedHashMap(settings.resources?.get(process) ?: [:])
    def units = context.containsKey('unit_resources') ? context.unit_resources : [:]
    if (units instanceof groovyx.gpars.dataflow.DataflowVariable) units = units.getVal()
    if (!(units instanceof Map)) error 'unit_resources must be a map or a value channel containing a map.'
    def selected = units.containsKey(meta.unit_id) ? units[meta.unit_id] : [:]
    if (!(selected instanceof Map)) error 'Each unit_resources entry must map process names to resource maps.'
    def overrides = selected.containsKey(process) ? selected[process] : [:]
    if (!(overrides instanceof Map) || overrides.keySet() - ['cpus','memory','disk','time'])
        error 'Unit resource overrides support only cpus, memory, disk and time.'
    resources + new LinkedHashMap(overrides)
}

def taskOptions(String name, Map settings, Map context, Map meta, Map overrides=[:]) {
    def ctx = stageContext(context)
    def result = processOptions(name, settings) + [unit_id:meta.unit_id, unit_meta:new LinkedHashMap(meta), owning_version:ctx.workflow_version]
    result.resources = unitResources(name,settings,context,meta)
    if (result.containsKey('results_path')) result.results_path = "${ctx.output_uri}/artifacts/${meta.unit_id}".toString()
    result + overrides
}

def boundaryOptions(String kind, Map settings, Map context, Map meta, boolean publish=true) {
    def ctx = stageContext(context)
    [unit_id:meta.unit_id, unit_meta:new LinkedHashMap(meta), stage:kind, publish:publish,
     container:kind == 'exported' ? settings.container_images.export_verifier : settings.container_images.base,
     resources:unitResources('bundle_stage',settings,context,meta),
     results_path:"${ctx.output_uri}/bundles".toString(), publish_overwrite:settings.publish_overwrite]
}

def bundleRecord(path) { meaReadJson(path.resolve('bundle.json')) }
def children(path) { path.toFile().listFiles().sort{ a,b -> a.name <=> b.name }.collect{ file(it.toString()) } }
def family(path, String prefix) { children(path.resolve('data')).findAll{ it.name.startsWith(prefix) } }
def rawSession(path) { path.resolve('data/ecephys_session') }
def duration(path) { bundleRecord(path).metadata.max_duration_minutes.toString() }
def keyedUnits(units) { units.map{ meta,path -> tuple(meta.unit_id,[meta:meta,path:path]) } }
def producer(cfg,execution,results,inputs) { [settings:cfg,execution:execution,results:results,inputs:inputs] }
def keyedProducers(outputs) { outputs.map{ cfg,execution,results,inputs -> tuple(cfg.unit_id,producer(cfg,execution,results,inputs)) } }

def assemble(String phase, Map meta, Map settings, Map context, previous, List pieces) {
    def paths = []
    def request = [schema_version:1,phase:phase,unit_id:meta.unit_id,unit_meta:new LinkedHashMap(meta),context:stageContext(context),pieces:[],processes:[]]
    if (previous != null) { request.previous=paths.size(); paths.add(previous) }
    pieces.each { piece ->
        def item = [results:paths.size(),destination:piece.get('destination','data'),role:piece.get('role','scientific')]
        paths.add(piece.results)
        item.inputs=paths.size(); paths.add(piece.inputs)
        if (piece.get('role') == 'collector') item.publication=piece.settings.results_path
        request.pieces.add(item)
        request.processes.add([name:piece.name,execution:piece.execution,
            capsule_pin:piece.settings.capsule_pin,args:piece.settings.get('args',''),job_args:piece.settings.get('job_args','')])
    }
    request.input_count=paths.size()
    tuple(boundaryOptions(phase,settings,context,meta),request,paths)
}

workflow MEA_PREPROCESSING {
    take: units; settings; context
    main:
    valid = stageInputs(units)
    calls = valid.map{ meta,path -> tuple(taskOptions('job_dispatch',settings,context,meta),path) }
    job_dispatch(calls.map{ cfg,path -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/job_dispatch/code"))),calls.map{ cfg,path -> path })
    dispatch_join = job_dispatch.out.unit_results.map{ cfg,execution,results,inputs,max_duration ->
        tuple(cfg.unit_id,producer(cfg,execution,results,inputs)+[duration:max_duration])
    }.join(keyedUnits(valid),failOnDuplicate:true,failOnMismatch:true)
    dispatch_requests = dispatch_join.map{ id,p,original ->
        def cfg = p.settings
        def request = [schema_version:1,phase:'dispatch',unit_id:cfg.unit_id,unit_meta:cfg.unit_meta,context:stageContext(context),
            pieces:[[results:0,inputs:1]],duration:2,source:3,input_count:4,
            processes:[[name:'job_dispatch',execution:p.execution,capsule_pin:cfg.capsule_pin,args:cfg.args]]]
        // Scratch stage-out can copy the producer's input layout. Retain the
        // original declared unit, just as later stages retain their predecessor.
        // The packer byte-checks producer-side copies as dependency aliases.
        tuple(boundaryOptions('nwb',settings,context,cfg.unit_meta,false),request,[p.results,p.inputs,p.duration,original.path])
    }
    PACK_DISPATCH(dispatch_requests,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    dispatched = PACK_DISPATCH.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
    prep_calls = dispatched.map{ meta,path -> tuple(taskOptions('preprocessing',settings,context,meta),path) }
    preprocessing(prep_calls.map{ cfg,path -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/preprocessing/code"))),
        prep_calls.map{ cfg,path -> duration(path) },prep_calls.map{ cfg,path -> rawSession(path) },prep_calls.map{ cfg,path -> family(path,'job_') })
    prepared_join = keyedProducers(preprocessing.out.unit_results).join(keyedUnits(dispatched),failOnDuplicate:true,failOnMismatch:true)
    requests = prepared_join.map{ id,p,prior -> assemble('prepared',prior.meta,settings,context,prior.path,[p+[name:'preprocessing']]) }
    PACK_PREPARED(requests,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    emit:
    units = PACK_PREPARED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
}

workflow MEA_KILOSORT4 {
    take: units; settings; context
    main:
    if (settings.sorter != 'kilosort4') error 'MEA_KILOSORT4 requires the Kilosort4 settings owner.'
    valid = stageInputs(units).map{ meta,path -> tuple(boundaryOptions('prepared',settings,context,meta,false),path) }
    VERIFY_PREPARED(valid,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    verified = VERIFY_PREPARED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
    calls = verified.map{ meta,path -> tuple(taskOptions('spikesort_kilosort4',settings,context,meta),path) }
    spikesort_kilosort4(calls.map{ cfg,path -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code"))),
        calls.map{ cfg,path -> duration(path) },calls.map{ cfg,path -> children(path.resolve('data')).findAll{ !it.name.equals('ecephys_session') } })
    joined = keyedProducers(spikesort_kilosort4.out.unit_results).join(keyedUnits(verified),failOnDuplicate:true,failOnMismatch:true)
    requests = joined.map{ id,p,prior -> assemble('sorted',prior.meta,settings,context,prior.path,[p+[name:'spikesort_kilosort4']]) }
    PACK_SORTED(requests,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    emit:
    units = PACK_SORTED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
}

workflow MEA_ANALYSIS {
    take: units; settings; context
    main:
    valid = stageInputs(units).map{ meta,path -> tuple(boundaryOptions('sorted',settings,context,meta,false),path) }
    VERIFY_SORTED(valid,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    verified = VERIFY_SORTED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
    calls = verified.map{ meta,path -> tuple(taskOptions('postprocessing',settings,context,meta),path) }
    postprocessing(calls.map{ cfg,path -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/postprocessing/code"))),
        calls.map{ cfg,path -> duration(path) },calls.map{ cfg,path -> rawSession(path) },calls.map{ cfg,path -> family(path,'job_') },
        calls.map{ cfg,path -> children(path.resolve('data')).findAll{ !it.name.equals('ecephys_session') && !it.name.startsWith('job_') && !it.name.startsWith('spikesorted_') && !it.name.startsWith('data_process_spikesorting_') } },
        calls.map{ cfg,path -> family(path,'spikesorted_') + family(path,'data_process_spikesorting_') })
    post = keyedProducers(postprocessing.out.unit_results).join(keyedUnits(verified),failOnDuplicate:true,failOnMismatch:true)
    cur_calls = post.map{ id,p,prior -> tuple(taskOptions('curation',settings,context,prior.meta),p,prior) }
    curation(cur_calls.map{ cfg,p,prior -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/curation/code"))),
        cur_calls.map{ cfg,p,prior -> duration(prior.path) },cur_calls.map{ cfg,p,prior -> children(p.results) })
    curated = keyedProducers(curation.out.unit_results).join(post,failOnDuplicate:true,failOnMismatch:true)
    viz_calls = curated.map{ id,c,p,prior -> tuple(taskOptions('visualization',settings,context,prior.meta),c,p,prior) }
    visualization(viz_calls.map{ cfg,c,p,prior -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/visualization/code"))),
        viz_calls.map{ cfg,c,p,prior -> duration(prior.path) },viz_calls.map{ cfg,c,p,prior -> rawSession(prior.path) },
        viz_calls.map{ cfg,c,p,prior -> family(prior.path,'job_') },
        viz_calls.map{ cfg,c,p,prior -> children(prior.path.resolve('data')).findAll{ !it.name.equals('ecephys_session') && !it.name.startsWith('job_') && !it.name.startsWith('spikesorted_') && !it.name.startsWith('data_process_spikesorting_') } },
        viz_calls.map{ cfg,c,p,prior -> family(prior.path,'spikesorted_') + family(prior.path,'data_process_spikesorting_') },
        viz_calls.map{ cfg,c,p,prior -> children(p.results) },viz_calls.map{ cfg,c,p,prior -> children(c.results) })
    visualized = keyedProducers(visualization.out.unit_results).join(curated,failOnDuplicate:true,failOnMismatch:true)
    collect_calls = visualized.map{ id,v,c,p,prior -> tuple(taskOptions('results_collector',settings,context,prior.meta,[input_path:rawSession(prior.path).toString()]),v,c,p,prior) }
    results_collector(collect_calls.map{ cfg,v,c,p,prior -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/results_collector/code"))),
        collect_calls.map{ cfg,v,c,p,prior -> duration(prior.path) },
        collect_calls.map{ cfg,v,c,p,prior -> rawSession(prior.path) },collect_calls.map{ cfg,v,c,p,prior -> family(prior.path,'job_') },
        collect_calls.map{ cfg,v,c,p,prior -> children(prior.path.resolve('data')).findAll{ !it.name.equals('ecephys_session') && !it.name.startsWith('job_') && !it.name.startsWith('spikesorted_') && !it.name.startsWith('data_process_spikesorting_') } },
        collect_calls.map{ cfg,v,c,p,prior -> family(prior.path,'spikesorted_') + family(prior.path,'data_process_spikesorting_') },
        collect_calls.map{ cfg,v,c,p,prior -> children(p.results) },collect_calls.map{ cfg,v,c,p,prior -> children(c.results) },collect_calls.map{ cfg,v,c,p,prior -> children(v.results) })
    collected = keyedProducers(results_collector.out.unit_results).join(visualized,failOnDuplicate:true,failOnMismatch:true)
    requests = collected.map{ id,r,v,c,p,prior -> assemble('analyzed',prior.meta,settings,context,prior.path,
        [p+[name:'postprocessing'],c+[name:'curation'],v+[name:'visualization'],r+[name:'results_collector',role:'collector',destination:'data/collected']]) }
    PACK_ANALYZED(requests,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    emit:
    units = PACK_ANALYZED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
}

workflow MEA_REPORTING {
    take: units; settings; context
    main:
    valid = stageInputs(units).map{ meta,path -> tuple(boundaryOptions('analyzed',settings,context,meta,false),path) }
    VERIFY_ANALYZED(valid,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    verified = VERIFY_ANALYZED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
    calls = verified.flatMap{ meta,path -> bundleRecord(path).recording_ids.collect{ id ->
        tuple(taskOptions('report_generation',settings,context,meta,[analyzer_subpath:"data/postprocessed_${id}.zarr".toString()]),tuple(id,path,[path])) } }
    report_generation(calls.map{ cfg,analyzer -> cfg },calls.map{ cfg,analyzer -> analyzer },channel.value(meaCodeInput(file("${moduleDir}/../capsules/report_generation"))))
    reports = report_generation.out.unit_results.map{ cfg,execution,id,results,inputs -> tuple(cfg.unit_id,id,producer(cfg,execution,results,inputs)) }
    burst_calls = reports.map{ unit,id,p -> tuple(taskOptions('burst_detection',settings,context,p.settings.unit_meta),tuple(id,p.results.resolve("${id}/spike_times.npy"))) }
    burst_detection(burst_calls.map{ cfg,times -> cfg },burst_calls.map{ cfg,times -> times },channel.value(meaCodeInput(file("${moduleDir}/../capsules/burst_detection"))))
    bursts = burst_detection.out.unit_results.map{ cfg,execution,id,results,inputs -> tuple(cfg.unit_id,id,producer(cfg,execution,results,inputs)) }
    report_groups = reports.groupTuple().map{ unit,ids,producers -> tuple(unit,[ids:ids,producers:producers]) }
    burst_groups = bursts.groupTuple().map{ unit,ids,producers -> tuple(unit,[ids:ids,producers:producers]) }
    joined = report_groups.join(burst_groups,failOnDuplicate:true,failOnMismatch:true).join(keyedUnits(verified),failOnDuplicate:true,failOnMismatch:true)
    requests = joined.map{ unit,r,b,prior ->
        def expected = bundleRecord(prior.path).recording_ids.toSet()
        if (r.ids.toSet()!=expected || b.ids.toSet()!=expected || r.ids.size()!=expected.size() || b.ids.size()!=expected.size()) error "Incomplete report/burst set for ${unit}"
        assemble('reported',prior.meta,settings,context,prior.path,
            r.producers.collect{ it+[name:'report_generation',destination:'data/reports'] } +
            b.producers.collect{ it+[name:'burst_detection',destination:'data/bursts'] })
    }
    PACK_REPORTED(requests,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    emit:
    units = PACK_REPORTED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
}

workflow MEA_FINAL_EXPORT {
    take: units; settings; context
    main:
    valid = stageInputs(units).map{ meta,path -> tuple(boundaryOptions('reported',settings,context,meta,false),path) }
    VERIFY_REPORTED(valid,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    verified = VERIFY_REPORTED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
    calls = verified.map{ meta,path -> tuple(taskOptions('nwb_ecephys',settings,context,meta),path) }
    nwb_ecephys(calls.map{ cfg,path -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/nwb_ecephys/code"))),
        channel.value(meaCodeInput(file("${moduleDir}/../capsules/nwb_common/code/source_nwb.py"))),
        channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/runtime/nwb_graph.py"))),calls.map{ cfg,path -> duration(path) },
        calls.map{ cfg,path -> rawSession(path) },calls.map{ cfg,path -> family(path,'job_') })
    raw = keyedProducers(nwb_ecephys.out.unit_results).join(keyedUnits(verified),failOnDuplicate:true,failOnMismatch:true)
    unit_calls = raw.map{ id,n,prior -> tuple(taskOptions('nwb_units',settings,context,prior.meta),n,prior) }
    nwb_units(unit_calls.map{ cfg,n,prior -> cfg },channel.value(meaCodeInput(file("${moduleDir}/../capsules/nwb_units/code"))),
        channel.value(meaCodeInput(file("${moduleDir}/../capsules/nwb_common/code/source_nwb.py"))),
        channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/runtime/nwb_graph.py"))),
        unit_calls.map{ cfg,n,prior -> duration(prior.path) },unit_calls.map{ cfg,n,prior -> rawSession(prior.path) },
        unit_calls.map{ cfg,n,prior -> family(prior.path,'job_') },unit_calls.map{ cfg,n,prior -> children(prior.path.resolve('data/collected')) },
        unit_calls.map{ cfg,n,prior -> children(n.results) })
    joined = keyedProducers(nwb_units.out.unit_results).join(raw,failOnDuplicate:true,failOnMismatch:true)
    requests = joined.map{ id,n,raw_nwb,prior ->
        def request=assemble('exported',prior.meta,settings,context,prior.path,[n+[name:'nwb_units',destination:'data/derived-nwb']])
        request[1].processes.add([name:'nwb_ecephys',execution:raw_nwb.execution,capsule_pin:raw_nwb.settings.capsule_pin,args:raw_nwb.settings.args])
        request
    }
    PACK_EXPORTED(requests,channel.value(meaCodeInput(file("${moduleDir}/helpers"))),channel.value(meaCodeInput(file("${moduleDir}/../capsules/kilosort4/code/outcomes.py"))),channel.value(meaCodeInput(file("${moduleDir}/components/maxwell/components/common/python"))))
    emit:
    units = PACK_EXPORTED.out.units.map{ cfg,path -> tuple(cfg.unit_meta,path) }
}
