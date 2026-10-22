nextflow.enable.dsl = 2
include { loadLaunchSettings } from '../../pipeline/settings'
include { taskOptions; boundaryOptions; unitResources } from '../../pipeline/stages'
include { meaReadJson } from '../../pipeline/json_values'
include { fullStageContext } from '../../pipeline/full'
params.resource_case = 'valid'
process RESOURCE_PLAN {
 output: path 'plan.json'
 script:
 def plan=[alpha:[preprocessing:[cpus:2,disk:'6 GB'],bundle_stage:[disk:'8 GB']],beta:[preprocessing:[disk:'7 GB']]]
 """
 cat > plan.json <<'DATA'
 ${groovy.json.JsonOutput.toJson(plan)}
 DATA
 """
}
process BUDGET_PROBE {
 tag { cfg.unit_id }
 cpus { cfg.resources.cpus ?: 1 }
 memory { cfg.resources.memory ?: '128 MB' }
 disk { cfg.resources.disk ?: '5 GB' }
 input: val cfg
 output: path "budget-${cfg.unit_id}.json"
 script:
 def result=[meta:cfg.unit_meta,resources:cfg.resources,observed:[cpus:task.cpus,memory:task.memory.toBytes(),disk:task.disk.toBytes()]]
 """
 cat > budget-${cfg.unit_id}.json <<'DATA'
 ${groovy.json.JsonOutput.toJson(result)}
 DATA
 """
}
workflow {
 def settings=loadLaunchSettings([ecephys_path:'/fixture',results_path:'/fixture',n_jobs:2,executor:'local',process_resources:[preprocessing:[cpus:1,memory:'128 MB',disk:'5 GB']]],"${projectDir}/../../pipeline")
 if (params.resource_case != 'valid') {
   def variants=['queue':channel.of([:]),'non-map':['invalid'],
       'unit-non-map':[alpha:'invalid'],'field':[alpha:[preprocessing:[scientific_option:true]]]]
   assert variants.containsKey(params.resource_case)
   unitResources('preprocessing',settings,[unit_resources:variants[params.resource_case]],[unit_id:'alpha'])
   error 'Invalid resource control was accepted.'
 }
 RESOURCE_PLAN()
 def resourceMap=RESOURCE_PLAN.out.map{meaReadJson(it)}.first()
 def ctx=[output_uri:'/fixture',workflow_version:'0.2.35',unit_resources:resourceMap]
 assert fullStageContext(ctx,'sorting').unit_resources.is(resourceMap)
 assert !fullStageContext(ctx,'conversion').containsKey('unit_resources')
 def original=groovy.json.JsonOutput.toJson(settings)
 def cfg=channel.of([unit_id:'alpha',unknown:[nullable:null,number:7.0]],[unit_id:'beta',unknown:[nullable:null,number:7.0]])
   .map {meta ->
      def call=taskOptions('preprocessing',settings,ctx,meta)
      def boundary=boundaryOptions('prepared',settings,ctx,meta)
      assert boundary.resources == (meta.unit_id=='alpha' ? [disk:'8 GB'] : [:])
      assert original==groovy.json.JsonOutput.toJson(settings)
      def staticCtx=ctx+[unit_resources:[(meta.unit_id):[preprocessing:[disk:'9 GB']]]]
      assert unitResources('preprocessing',settings,staticCtx,meta).disk=='9 GB'
      assert unitResources('preprocessing',settings,ctx+[unit_resources:[:]],meta).disk=='5 GB'
      assert meta.unknown.number==7.0 && meta.unknown.containsKey('nullable')
      assert unitResources('preprocessing',settings,ctx+[unit_resources:[:]],meta)==settings.resources.preprocessing
      call
   }
 BUDGET_PROBE(cfg)
 BUDGET_PROBE.out.view{'BUDGET_FILE='+it}
}
