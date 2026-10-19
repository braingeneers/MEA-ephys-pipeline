#!/usr/bin/env python3
"""Exercise exact current-invocation binding without changing producer receipts.

These are synthetic in-memory counterexamples derived from actual frozen graph
observations. Complete scientific/physical corruption controls remain separately
owned by controls.py and artifact_controls.py; they are not replaced here.
"""
from pathlib import Path
import argparse
import base64
import copy
import json
import sys

import h5py

import current_graph as graph
from canonical import require, load_array
from inputs import sha, typed


def verify(scope, output):
    output.mkdir(parents=True,exist_ok=False)
    results = []

    def reject(label, value, operation, intended):
        before = typed(value)
        try:
            operation(value)
        except ValueError as error:
            require(intended in str(error), label+': intended comparison, got '+str(error))
            result = dict(control=label,rejected=True,intended_comparison=intended,error=str(error))
        else:
            raise ValueError(label+': changed evidence was accepted')
        require(typed(value) == before, label+': checker did not mutate its input')
        (output/(label+'.json')).write_text(json.dumps(dict(result=result,synthetic_counterexample=value),indent=2)+'\n')
        results.append(result)

    for alias in ('alpha','beta'):
        row = next(value for value in scope.execution['complete_phase_observations']
                   if value['kind']=='mea' and value['alias']==alias and value['case']=='maxone')
        folder = scope.root/'inputs'/row['case']; facts = scope.read(folder/'facts.json')
        receipt = graph.observations(scope,row,facts)
        paths = graph.publication(scope,row)
        selected, = [value for value in facts['stores'] if value['selected']]
        mutations = [
            ('owner-version',lambda r:r['native_plan']['semantic']['owner'].update(workflow_version='9.9.9'),'Current scientific owner binding'),
            ('owner-image',lambda r:r['evidence']['owner'].update(image='foreign:1'),'Current scientific owner binding'),
            ('source-hash',lambda r:r['task']['raw'].update(sha256='0'*64),'Current source/projection identity binding'),
            ('projection-hash',lambda r:r['task']['unit']['projection'].update(sha256='0'*64),'Current source/projection identity binding'),
            ('raw-selection',lambda r:r['task'].update(raw_dataset_path='/data_store/data9999/groups/routed/raw'),'Current selected raw binding'),
            ('settings-effective',lambda r:r['effective'].update(nwb_buffer_chunks=11),'Complete current settings binding'),
            ('settings-submitted',lambda r:r['submitted'].update(nwb_buffer_chunks=10.0),'Complete current settings binding'),
            ('evidence-submitted',lambda r:r['evidence']['settings']['submitted'].update(nwb_buffer_chunks=10),'Complete current settings binding'),
            ('workdir-path',lambda r:r.update(task_workdir='/foreign/task'),'Current owned task directory'),
            ('argv',lambda r:r['phase_observations'][1]['command'].append('--foreign-option'),'Exact current CLI/options: convert'),
            ('child-exit',lambda r:r['phase_observations'][0].update(actual_child_exit_from_owning_report=1),'Actual current scientific child exit'),
            ('program-hash',lambda r:r['phase_observations'][0]['program_identity'].update(sha256='0'*64),'Actual current program bytes'),
            ('phase-order',lambda r:r['phase_observations'].reverse(),'Complete current phase order'),
            ('request-bytes',lambda r:r['phase_observations'][-1]['files_before'][str(Path(r['task_workdir'])/'scientific-task/checks-request.json')].update(content_b64=base64.b64encode(b'{}').decode()),'Actual current validator request/result bytes'),
            ('result-bytes',lambda r:r['phase_observations'][-1]['files_after'][str(Path(r['task_workdir'])/'scientific-task/checks.json')].update(content_b64=base64.b64encode(b'{}').decode()),'Actual current validator request/result bytes'),
            ('final-sha',lambda r:r['phase_observations'][-1]['files_before'][str(Path(r['task_workdir'])/'scientific-task/output.nwb')].update(sha256='0'*64),'Current final NWB before/after validate_outputs'),
        ]
        for name, mutate, intended in mutations:
            value = copy.deepcopy(row); mutate(value)
            require(typed(value) != typed(row), name+': meaningful changed observation')
            reject(alias+'-'+name,value,lambda r:graph.observations(scope,r,facts),intended)
        for field,wrong in (('size',1),('sha256','0'*64),('uri',row['task']['outputs']['lindi'])):
            value = copy.deepcopy(row)
            next(v for v in value['evidence']['artifacts'] if v['kind']=='nwb')['object'][field] = wrong
            reject(alias+'-publication-'+field,value,lambda r:graph.publication(scope,r),'Actual current publication URI/size/SHA')
        value = copy.deepcopy(row);value['task']['outputs']['nwb']='file:///foreign/output.nwb'
        reject(alias+'-publication-path',value,lambda r:graph.publication(scope,r),'Scoped current regular file')

        sidecar = scope.read(paths['lindi'])
        first_range = next(key for key,value in sidecar['refs'].items() if isinstance(value,list))
        header_mutations = [
            ('template',lambda s:s['templates'].update(u1='file:///foreign/output.nwb'),'Exact current single-source template representation'),
            ('range-url',lambda s:s['refs'][first_range].__setitem__(0,'file:///foreign/output.nwb'),'Exact current range URL representation'),
            ('metadata-final-path',lambda s:s['generationMetadata'].update(sourceNwbFile='/foreign/converted.nwb'),'Complete current LINDI generation metadata'),
            ('metadata-url',lambda s:s['generationMetadata'].update(sourceUrl='file:///foreign/output.nwb'),'Complete current LINDI generation metadata'),
            ('metadata-extra',lambda s:s['generationMetadata'].update(unreviewed=True),'Complete current LINDI generation metadata'),
            ('version-type',lambda s:s.update(version=1.0),'Complete current sidecar root inventory/version'),
        ]
        for name,mutate,intended in header_mutations:
            value = copy.deepcopy(sidecar);mutate(value)
            reject(alias+'-'+name,value,lambda s:graph.current_sidecar_header(s,receipt,selected),intended)
        # A format-valid floating byte offset must reach the native range type guard.
        value = copy.deepcopy(sidecar);value['refs'][first_range][1] = float(value['refs'][first_range][1])
        from maxwell_lindi_codec import MaxwellMxwDataCodec
        truth = load_array(folder,selected['signal'])
        with h5py.File(paths['nwb'],'r') as handle:
            reject(alias+'-range-offset-type',value,
                   lambda s:graph.current_sidecar(handle,paths['nwb'],s,receipt,MaxwellMxwDataCodec(),truth,selected),
                   'native byte reference field types')
        # Positive observations must still pass after all synthetic controls.
        require(graph.observations(scope,row,facts) == receipt,'Unchanged observation after controls')
        graph.publication(scope,row);graph.current_sidecar_header(sidecar,receipt,selected)

    reject('untrusted-seal','0'*64,lambda value:graph.CurrentGraphScope(scope.root,value),'Trusted current graph seal')
    result = dict(passed=True,controls=len(results),cases=results,
                  original_receipts_and_artifacts_modified=False,
                  positive_observations_pass_before_and_after=True,
                  scientific_corruption_controls_replaced=False)
    (output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:value for key,value in result.items() if key!='cases'}),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--trusted-freeze-sha256',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args = parser.parse_args()
    sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    verify(graph.CurrentGraphScope(args.root,args.trusted_freeze_sha256),args.output)
