#!/usr/bin/env python3
"""Validate input-only facts with no converted NWB or producing work available."""
from __future__ import annotations
import argparse,hashlib,importlib.metadata,json,math,re,sys
from datetime import datetime,timezone
from pathlib import Path
import h5py
import numpy as np
from spikeinterface.extractors.extractor_classes import MaxwellRecordingExtractor
from pynwb import NWBFile
from hdmf.container import Data
import ndx_cell_culture
from inputs import SCHEMA,sha,typed

sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
from maxwell_lindi_codec import MaxwellMxwDataCodec
from cell_culture_projection import validate_projection_export,apply_cell_culture_projection


def array(root,descriptor):
 path=root/descriptor['file'];assert sha(path)==descriptor['sha256'],path
 value=np.load(path,allow_pickle=False)
 assert list(value.shape)==descriptor['shape'] and value.dtype.str==descriptor['dtype'],path
 if descriptor.get('dtype_fields') is not None:
  assert json.loads(json.dumps(value.dtype.descr))==descriptor['dtype_fields'],path
 return value


def model_constructor_check(model,facts):
 """Acceptance-only constructor observation; never derives or changes expected facts."""
 validate_projection_export(model)
 nwb=NWBFile('Input constructor validation','input-only',datetime(2026,10,18,tzinfo=timezone.utc))
 nwb.create_device('DeviceEcephys');apply_cell_culture_projection(nwb,model)
 subject=nwb.subject;context=nwb.lab_meta_data['experiment_context'];objects={'subject':subject,'context':context}
 def name(kind,key):return kind+'-'+key+'-'+hashlib.sha256(key.encode()).hexdigest()[:16]
 assert subject.related_cultures is subject.cell_cultures
 assert len(subject.cell_cultures)==3 and len(subject.cell_lines)==2
 for key in model['cell_lines']:objects['lines/'+key]=subject.cell_lines[name('line',key)]
 for key in model['cultures']:objects['cultures/'+key]=subject.cell_cultures[name('culture',key)]
 source=objects['lines/source-line']
 objects['lines/source-line/construct_applications/application-one']=source.construct_applications[name('application','application-one')]
 objects['lines/source-line/genetic_variants/variant-one']=source.genetic_variants[name('variant','variant-one')]
 objects['cultures/recorded/protocol']=objects['cultures/recorded'].culture_protocol
 for key in ['vehicle','washout']:objects['pharmacologies/'+key]=context.pharmacologies[key]
 assert len(objects)==len(facts['model']['nodes'])==12
 assert subject.culture is objects['cultures/recorded']
 assert context.device is nwb.devices['DeviceEcephys']
 vectors=0
 for row in facts['model']['Data_reference_payloads']:
  value=getattr(objects[row['owner']],row['field']);assert isinstance(value,Data)
  assert value.description==row['description'] and len(value.data)==row['shape'][0]
  assert all(a is objects[b] for a,b in zip(value.data,row['targets']))
  vectors+=1
 assert vectors==4
 assert objects['cultures/parent-a'] is not objects['cultures/parent-b']
 assert objects['cultures/parent-a'].culture_id==objects['cultures/parent-b'].culture_id=='shared-parent'
 assert objects['cultures/parent-a'].age=='P60D' and objects['cultures/parent-b'].age=='P75D'
 for key,node in facts['model']['nodes'].items():
  obj=objects[key];assert obj.neurodata_type==node['kind'],key
  # Authored encodings, schema minimum precision and the pinned writer's exact
  # binary64 storage rule are distinct contracts. No converted NWB is read here.
  for field,expected in node['fields']['fields'].items():
   actual=getattr(obj,field)
   if 'context/'+field in facts['model']['numeric_storage_normalization'] and key=='context':
    assert type(actual) is float and actual.hex()==float(model['context'][field]).hex()
   elif key.startswith('pharmacologies/') and field in ['start_time_s','end_time_s']:
    original=next(x for x in model['context']['pharmacology'] if x['pharmacology_id']==key.split('/')[1])[field]
    assert type(actual) is float and actual.hex()==float(original).hex()
   else:assert typed(actual)==expected,(key,field,typed(actual),expected)
  if key.startswith('cultures/') and '/protocol' not in key:assert obj.parent is subject,key
  if key.startswith('lines/') and key.count('/')==1:assert obj.parent is subject,key
 assert objects['lines/source-line/genetic_variants/variant-one'].related_application is objects['lines/source-line/construct_applications/application-one']
 assert len(context.pharmacologies)==2
 return dict(input_model_constructors_passed=True,authored_model_nodes_checked=12,reference_Data_payloads_checked=4,
  repeated_biological_ids_preserve_distinct_authored_objects=True,related_cultures_alias_includes_recorded=True,
  expected_values_derived_from_constructor=False,converted_nwb_schema_validity_claimed=False)


def scalar_storage_check(model,facts):
 """Check every annotation against original authored JSON, not converted output."""
 expected={}
 for field in ['temperature_c','recording_duration_s']:
  expected['context/'+field]=model['context'][field]
 for row in model['context']['pharmacology']:
  for field in ['start_time_s','end_time_s']:
   expected['pharmacologies/'+row['pharmacology_id']+'/'+field]=row[field]
 actual=facts['model']['numeric_storage_normalization']
 assert set(actual)==set(expected)
 for key,value in expected.items():
  assert type(value) in [int,float] and math.isfinite(value),key
  assert actual[key]==dict(authored=typed(value),declared_schema_dtype='<f4',
   stored_dtype='<f8',stored_float_hex=float(value).hex()),key
 return dict(annotations_checked=len(expected),declared_schema_dtype='<f4',
  required_stored_dtype='<f8',exact_original_binary64_values_required=True)


def verify(root):
 configure_bundled_maxwell_hdf5_plugin();codec=MaxwellMxwDataCodec()
 source_manifest=json.loads((root/'input-set.json').read_text());assert source_manifest['schema_version']==SCHEMA
 results=[];total_chunks=total_values=selected_values=0
 for case in source_manifest['cases']:
  folder=root/case['case'];facts=json.loads((folder/'facts.json').read_text());assert sha(folder/'facts.json')==case['facts_sha256']
  assert facts['schema_version']==SCHEMA
  source=folder/facts['source']['file'];projection=folder/facts['projection']['file']
  assert sha(source)==facts['source']['sha256'] and source.stat().st_size==facts['source']['size']
  assert sha(projection)==facts['projection']['sha256'] and projection.stat().st_size==facts['projection']['size']
  model=json.loads(projection.read_text());assert typed(model)==facts['projection']['original_typed']
  stores=[]
  with h5py.File(source,'r') as handle,source.open('rb') as stream:
   for store in facts['stores']:
    raw=handle[store['raw_dataset_path']];expected=array(folder,store['signal']);clock=array(folder,store['clock']);mapping=array(folder,store['mapping'])
    assert raw.dtype==expected.dtype and raw.shape==expected.T.shape
    assert raw[:].T.tobytes()==expected.tobytes()
    group=handle[store['store']]
    assert group['settings/mapping'][:].tobytes()==mapping.tobytes()
    assert handle['wells/'+store['well']+'/rec0000'].id==group.id
    assert handle['recordings/rec0000/'+store['well']].id==group.id
    assert float(group['settings/lsb'][0]).hex()==store['source_lsb_hex']
    assert int(group['settings/sampling'][0])==store['rate_hz']==20000
    assert not raw.is_virtual and not raw.external and raw.chunks==(4,200)
    pipeline=[raw.id.get_create_plist().get_filter(i) for i in range(raw.id.get_create_plist().get_nfilters())]
    assert len(pipeline)==1 and pipeline[0][0]==401
    assert raw.id.get_num_chunks()==len(store['source_chunks'])
    for row in store['source_chunks']:
     info=raw.id.get_chunk_info_by_coord(tuple(row['coordinate']));mask,payload=raw.id.read_direct_chunk(tuple(row['coordinate']))
     assert mask==row['filter_mask']==0 and info.byte_offset==row['offset'] and len(payload)==row['bytes']
     assert hashlib.sha256(payload).hexdigest()==row['sha256']
     stream.seek(row['offset']);assert stream.read(row['bytes'])==payload
     values=np.frombuffer(codec.decode(payload),dtype='<u2').reshape(raw.chunks)
     start=row['coordinate'][1];count=min(200,raw.shape[1]-start)
     assert values[:,:count].T.tobytes()==expected[start:start+count].tobytes()
    recording=MaxwellRecordingExtractor(file_path=str(source),stream_id=store['well'],rec_name='rec0000')
    actual=recording.get_traces();assert actual.shape==expected.shape and actual.dtype==expected.dtype and actual.tobytes()==expected.tobytes()
    actual_clock=recording.get_times();assert actual_clock.dtype==clock.dtype and actual_clock.shape==clock.shape and actual_clock.tobytes()==clock.tobytes()
    assert all(float(x).hex()==store['gain_microvolts_hex'] for x in recording.get_channel_gains())
    assert all(float(x).hex()==store['offset_hex'] for x in recording.get_channel_offsets())
    locations=recording.get_channel_locations();expected_locations=np.column_stack((mapping['x'],mapping['y']))
    assert locations.dtype==expected_locations.dtype and locations.shape==expected_locations.shape and locations.tobytes()==expected_locations.tobytes()
    assert recording.get_channel_ids().tolist()==['0','1','2','3']
    assert recording.get_property('channel_name').tolist()==store['electrodes']['columns']['channel_name']['values']
    assert recording.get_property('electrode').dtype==np.dtype('<i4')
    np.testing.assert_array_equal(recording.get_property('electrode'),array(folder,store['electrodes']['columns']['electrode']))
    assert recording.get_channel_groups().tolist()==[0,0,0,0]
    assert all(probe.contact_ids is None for probe in recording.get_probegroup().probes)
    for field in ['id','rel_x','rel_y']:array(folder,store['electrodes']['columns'][field])
    assert set(store['electrodes']['columns'])=={'id','group','group_name','location','channel_name','electrode','rel_x','rel_y'}
    total_chunks+=len(store['source_chunks']);total_values+=expected.size
    if store['selected']:selected_values+=expected.size
    stores.append(dict(well=store['well'],store=store['store'],selected=store['selected'],shape=list(raw.shape),
     input_values_checked=int(expected.size),chunks=len(store['source_chunks']),all_native_ranges_and_source_readers_match=True,
     reader_property_keys=recording.get_property_keys(),declared_electrode_Data_payloads=8))
  if facts['case']=='maxtwo_routed':assert next(x for x in facts['stores'] if x['selected'])['store']=='/data_store/data0001'
  check=model_constructor_check(model,facts);scalar_check=scalar_storage_check(model,facts)
  results.append(dict(case=facts['case'],source_sha256=sha(source),projection_sha256=sha(projection),stores=stores,model=check,scalar_storage=scalar_check))
 recordings=json.loads((root/'recordings.json').read_text());bindings=json.loads((root/'bindings.json').read_text());transport=json.loads((root/'transport-facts.json').read_text())
 assert transport==dict(recordings=typed(recordings),bindings=typed(bindings))
 for value in [r['unknown_typed'] for r in recordings]+[r['metadata']['unknown_typed'] for r in bindings]:
  assert type(value['integer']) is int and type(value['floating']) is float
  assert value['large_integer']==2**80+17 and value['negative_integer']==-(2**80+17)
  assert type(value['signed_zero']) is float and math.copysign(1.0,value['signed_zero'])<0
  assert type(value['bool']) is bool and value['null'] is None
 assert importlib.metadata.version('ndx-cell-culture')=='1.0.0'
 spec=Path(ndx_cell_culture.__file__).parent/'spec/ndx-cell-culture.extensions.yaml';text=spec.read_text()
 for kind,names in [('ExperimentContext',['temperature_c','recording_duration_s']),('Pharmacology',['start_time_s','end_time_s'])]:
  block=text.split('- neurodata_type_def: '+kind+'\n',1)[1].split('- neurodata_type_def:',1)[0]
  for name in names:assert re.search(r'- name: '+name+r'\n\s+dtype: float32',block)
 return dict(passed=True,cases=results,total_input_values_checked=int(total_values),selected_values=int(selected_values),
  native_source_chunks_checked=total_chunks,all_three_routed_stores_and_unselected_input_retained=True,
  original_json_numeric_types_signs_large_integers_and_unknowns_preserved=True,ndx_schema_sha256=sha(spec),
  converted_outputs_available=False,complete_checker_proven=False,conversion_parity_claimed=False)

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--inputs',required=True,type=Path);parser.add_argument('--report',required=True,type=Path)
 args=parser.parse_args();report=verify(args.inputs)
 with args.report.open('x') as file:file.write(json.dumps(report,indent=2)+'\n')
 print(json.dumps({k:v for k,v in report.items() if k!='cases'},indent=2))
