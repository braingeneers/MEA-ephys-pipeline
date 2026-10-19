#!/usr/bin/env python3
"""Generate raw Maxwell inputs and independent facts; never reads converted NWBs."""
from __future__ import annotations
import argparse,hashlib,json,math,sys
from pathlib import Path
import h5py
import numpy as np

SCHEMA=2
RATE=20000
FRAMES={'maxone':2003,'maxtwo_routed':2407}
DTYPE=np.dtype([('channel','<i4'),('electrode','<i4'),('x','<f8'),('y','<f8')])

def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as stream:
  for block in iter(lambda:stream.read(8*1024*1024),b''):h.update(block)
 return h.hexdigest()

def write(path,value):
 path.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n')

def typed(value):
 if value is None:return {'type':'null'}
 if type(value) is bool:return {'type':'bool','value':value}
 if type(value) is int:return {'type':'integer','decimal':str(value)}
 if type(value) is float:
  if not math.isfinite(value):raise ValueError('Facts require finite JSON floats')
  return {'type':'float64','hex':value.hex()}
 if type(value) is str:return {'type':'string','value':value}
 if isinstance(value,list):return {'type':'array','values':[typed(v) for v in value]}
 if isinstance(value,dict):return {'type':'object','fields':{k:typed(v) for k,v in value.items()}}
 raise TypeError(type(value))

def samples(store,frames):
 # The formula is input truth, evaluated before any HDF5 writer/reader is used.
 return np.asarray([[500+31*c+200*store+((7*t+t//17+13*c)%97)-48
                     for c in range(4)] for t in range(frames)],dtype='<u2')

def projection(case):
 parent=lambda age,label:dict(culture_id='shared-parent',culture_type='organoid',
  sample_label=label,species='Mus musculus',source_line_ids=['derived-line'],
  parent_culture_ids=[],age=age,age_reference='birth')
 return dict(projection_schema_version=1,dataset_uuid='oracle-'+case,
  experiment_name='experiment-'+case,unit='recording' if case=='maxone' else 'well002',
  recording_id=case,well_label='well000' if case=='maxone' else 'well002',recorded_culture_id='recorded',
  common=dict(lab='Generated laboratory',institution='Generated institution',
   experiment_description='Independent converter oracle',keywords=['oracle','μV'],
   related_publications=['https://example.invalid/oracle'],notes='Authored common notes',protocol='Generated acquisition'),
  people={'first':{'name':'Fixture, Alpha'},'second':{'name':'Fixture, Beta'}},
  cell_lines={
   'source-line':dict(cell_line_id='line-source',cell_line_type='other',sample_label='Source line',
    species='Mus musculus',cell_source_type='other',cell_line_name='Line source',passage_number='12',notes='Line notes',
    construct_applications=[dict(application_id='application-one',vector_type='other',payload='GENE1',
     promoter='generated',age_modified='P5D',notes='Application notes')],
    genetic_variants=[dict(variant_id='variant-one',target_symbol='GENE1',edit_type='other',origin='unknown',
     target_coordinates='chr1:101-102',related_application_id='application-one',notes='Variant notes')]),
   'derived-line':dict(cell_line_id='line-derived',cell_line_type='other',sample_label='Derived line',
    species='Mus musculus',cell_source_type='other',parent_cell_line_id='source-line')},
  cultures={'parent-a':parent('P60D','Parent at day 60'),'parent-b':parent('P75D','Parent at day 75'),
   'recorded':dict(culture_id='recorded-culture',culture_type='assembloid',sample_label='Recorded assembloid',
    species='Mus musculus',source_line_ids=[],parent_culture_ids=['parent-a','parent-b'],age='P90D',
    age_reference='birth',notes='Culture notes',culture_protocol=dict(protocol_id='protocol-one',
     protocol_name='Independent protocol',protocol_version='1',media_summary='Generated media'))},
  subject=dict(subject_id='subject-'+case,culture_id='recorded',age='P90D',species='Mus musculus',
   sex='U',strain='C57BL/6',description='Generated subject'),
  context=dict(experiment_id='context-'+case,session_id=case+'-session',age_at_recording='P90D',
   recording_platform='MEA',session_start_time='2026-10-18T00:00:00+00:00',
   session_description='Authored '+case+' description',temperature_c=37,
   recording_duration_s=FRAMES[case]/RATE,spontaneous_activity=True,electrical_stimulation=False,
   optical_stimulation=False,pharmacology_present=True,media_or_bath='Generated media',notes='Context notes',
   pharmacology=[dict(pharmacology_id='vehicle',agent='vehicle',start_time_s=0,end_time_s=0.05,
    concentration='0',concentration_unit='uM',purpose='control'),
    dict(pharmacology_id='washout',agent='washout',start_time_s=0.05,end_time_s=FRAMES[case]/RATE,notes='Washout notes')]))

def _node(kind,key,fields,references=None,contained=None):
 return dict(kind=kind,logical_key=key,fields=typed(fields),references=references or {},contained=contained or {})

def model_contract(model):
 """Declare logical schema semantics from authored inputs, without a writer call.

UUIDs are producer-generated, not biological keys. Ordered reference targets retain
 authored keys independently of repeated culture IDs and their distinct ages.
 """
 nodes={}
 for key,row in model['cell_lines'].items():
  fields={k:v for k,v in row.items() if k not in ['parent_cell_line_id','construct_applications','genetic_variants']}
  refs={'parent_cell_line':['lines/'+row['parent_cell_line_id']]} if 'parent_cell_line_id' in row else {}
  child={}
  for name,kind,id_field in [('construct_applications','ConstructApplication','application_id'),('genetic_variants','GeneticVariant','variant_id')]:
   targets=[]
   for entry in row.get(name,[]):
    ident=entry[id_field];logical='lines/'+key+'/'+name+'/'+ident;targets.append(logical)
    scalar={k:v for k,v in entry.items() if k!='related_application_id'}
    related={'related_application':['lines/'+key+'/construct_applications/'+entry['related_application_id']]} if 'related_application_id' in entry else {}
    nodes[logical]=_node(kind,ident,scalar,related)
   if targets:child[name]=targets
  nodes['lines/'+key]=_node('CellLine',key,fields,refs,child)
 for key,row in model['cultures'].items():
  fields={k:v for k,v in row.items() if k not in ['source_line_ids','parent_culture_ids','culture_protocol']}
  refs={field:[prefix+x for x in row.get(source,[])] for source,field,prefix in [('source_line_ids','source_lines','lines/'),('parent_culture_ids','parent_cultures','cultures/')] if row.get(source)}
  child={}
  if 'culture_protocol' in row:
   logical='cultures/'+key+'/protocol';value=row['culture_protocol'];nodes[logical]=_node('CultureProtocol',value['protocol_id'],value);child['culture_protocol']=[logical]
  nodes['cultures/'+key]=_node('CellCulture',key,fields,refs,child)
 recorded=model['recorded_culture_id']
 nodes['subject']=_node('CellCultureSubject','subject',{k:v for k,v in model['subject'].items() if k!='culture_id'},
  {'culture':['cultures/'+recorded]},dict(cell_lines=['lines/'+k for k in model['cell_lines']],
   cell_cultures=['cultures/'+k for k in model['cultures']]))
 context=model['context'];fields={k:v for k,v in context.items() if k not in ['pharmacology','session_id','session_start_time','session_description']}
 # ndx-cell-culture1.0.0 declares float32 minimum precision. The pinned producer
 # coerces these values to Python float; HDMF6.1.0 preserves its binary64 precision.
 # Keep original encodings separate from the exact source-derived storage rule.
 normalization={}
 for field in ['temperature_c','recording_duration_s']:
  normalization['context/'+field]=dict(authored=typed(fields[field]),declared_schema_dtype='<f4',stored_dtype='<f8',stored_float_hex=float(fields[field]).hex())
 for row in context['pharmacology']:
  ident=row['pharmacology_id'];nodes['pharmacologies/'+ident]=_node('Pharmacology',ident,row)
  for field in ['start_time_s','end_time_s']:
   normalization['pharmacologies/'+ident+'/'+field]=dict(authored=typed(row[field]),declared_schema_dtype='<f4',stored_dtype='<f8',stored_float_hex=float(row[field]).hex())
 nodes['context']=_node('ExperimentContext','experiment_context',fields,{'device':['device']},
  {'pharmacologies':['pharmacologies/'+x['pharmacology_id'] for x in context['pharmacology']]})
 return dict(nodes=nodes,numeric_storage_normalization=normalization,
  logical_identity='Authored collection key plus containment; never biological-ID equality or a generated UUID.',
  reference_order_and_cardinality_required=True,all_schema_fields_and_Data_payloads_required=True,
  Data_reference_payloads=[dict(owner=key,field=field,targets=targets,kind='VectorData',namespace='hdmf-common',shape=[len(targets)],description={'parent_cell_line':'Single immediate parent CellLine for a derived line, if known.','source_lines':'Source CellLine or lines used to create this culture.','parent_cultures':'Parent or input CellCulture objects used to derive this culture.'}[field]) for key,node in nodes.items() for field,targets in node['references'].items() if field in ['parent_cell_line','source_lines','parent_cultures']],
  derived_relations={'CellCultureSubject.related_cultures':'Public alias of cell_cultures; includes recorded and parent cultures.','ExperimentContext.pharmacologies':'Public alias of pharmacologys; same contained objects.','Pharmacology.experiment':'Owning ExperimentContext via parent; not a stored extra link.'},
  physical_alias_policy='Verify actual same-object identity; baseline expanded aliases need the separate complete within-file content/reference proof.',
  producer_generated_fields=dict(object_id='Unique valid UUID per canonical container; references must target the exact canonical object.',
    file_create_date='Measured invocation timestamps; verify separately against invocation evidence, never compare as authored science.'),
  remaining_checker_obligations=['Complete NWBFile/device/electrode node/field inventory from pinned interface/schema.',
   'Every electrode Data column and reference plus every signal attribute, dtype/shape and sample.',
   'Complete canonical storage, physical aliases, actual filter pipeline/masks/order and every native LINDI range.',
   'Companion mapping, sibling/URL inventory, verified publication and final-repacked validation report identity.'])

def describe_array(directory,name,array):
 path=directory/name;np.save(path,array,allow_pickle=False)
 return dict(file=name,sha256=sha(path),dtype=array.dtype.str,dtype_fields=array.dtype.descr if array.dtype.names else None,shape=list(array.shape))

def electrode_contract(directory,prefix,mapping):
 # Values originate in the acquisition mapping. Column rules are the pinned
 # NeuroConv0.9.3 public input-interface ABI, not a converted NWB observation.
 count=len(mapping)
 columns={
  'id':describe_array(directory,prefix+'-electrode-ids.npy',np.arange(count,dtype='<i8')),
  'electrode':describe_array(directory,prefix+'-electrode-numbers.npy',mapping['electrode']),
  'rel_x':describe_array(directory,prefix+'-electrode-x.npy',mapping['x']),
  'rel_y':describe_array(directory,prefix+'-electrode-y.npy',mapping['y']),
  'channel_name':dict(kind='utf8',shape=[count],values=['ch'+str(c)+' elec'+str(e) for c,e in zip(mapping['channel'],mapping['electrode'])]),
  'group_name':dict(kind='utf8',shape=[count],values=['0']*count),
  'location':dict(kind='utf8',shape=[count],values=['unknown']*count),
  'group':dict(kind='canonical_reference',shape=[count],targets=['electrode_groups/0']*count)}
 return dict(columns=columns,column_inventory_exact=True,rows=count,
  table=dict(name='electrodes',namespace='core',kind='ElectrodesTable'),
  signal_region=dict(kind='DynamicTableRegion',name='electrodes',dtype='<i8',rows=list(range(count)),table='electrodes'),
  probe_electrode_name_column=False,
  gain_offset_and_physical_unit_properties='Represented in signal calibration, not duplicate table columns.',
  geometry_units='micrometers',all_VectorData_payloads_required=True)


def prepare(root,uri_prefix='file:///inputs/'):

 if root.exists():raise ValueError('Inputs require a new output directory')
 if not uri_prefix.startswith('file:///') or not uri_prefix.endswith('/'):raise ValueError('Explicit local fixture URI prefix required')
 sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
 from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
 configure_bundled_maxwell_hdf5_plugin()
 root.mkdir(parents=True);recordings=[];bindings=[];summaries=[]
 for case,frames in FRAMES.items():
  directory=root/case;directory.mkdir();source=directory/'recording.raw.h5';stores=[]
  with h5py.File(source,'w') as handle:
   handle.create_dataset('version',data=[b'20190530'])
   for index,well in enumerate(['well000'] if case=='maxone' else ['well000','well002']):
    count=frames if index or case=='maxone' else 2003
    matrix=samples(index,count);mapping=np.zeros(4,dtype=DTYPE)
    mapping['channel']=np.arange(4);mapping['electrode']=100+3*np.arange(4)
    mapping['x']=17.5*np.arange(4)+50*index;mapping['y']=11.25*np.arange(4)+25*index
    group=handle.create_group(f'data_store/data{index:04d}')
    group.create_dataset('settings/sampling',data=[RATE]);group.create_dataset('settings/lsb',data=[6.3e-6]);group.create_dataset('settings/mapping',data=mapping)
    raw=group.create_dataset('groups/routed/raw',data=matrix.T,chunks=(4,200),compression=401)
    handle.require_group('wells/'+well)['rec0000']=group;handle.require_group('recordings/rec0000')[well]=group
    prefix='store'+str(index)
    stores.append(dict(store=f'/data_store/data{index:04d}',well=well,raw_dataset_path=raw.name,
     selected=index==(0 if case=='maxone' else 1),signal=describe_array(directory,prefix+'-signal.npy',matrix),
     clock=describe_array(directory,prefix+'-clock.npy',np.arange(count,dtype='<f8')/RATE),
     mapping=describe_array(directory,prefix+'-mapping.npy',mapping),
     source_lsb_hex=float(6.3e-6).hex(),gain_microvolts_hex=float(6.3e-6*1e6).hex(),
     expected_nwb_conversion_hex=float((6.3e-6*1e6)*1e-6).hex(),offset_hex=0.0.hex(),rate_hz=RATE,
     electrodes=electrode_contract(directory,prefix,mapping),
     input_chunks=[4,200],expected_nwb_dtype='<u2',expected_nwb_shape=[count,4],coordinate_units='micrometers'))
  # Input chunk identities are source facts, not conversion-output expectations.
  with h5py.File(source) as handle:
   for entry in stores:
    raw=handle[entry['raw_dataset_path']];entry['input_filter_pipeline']=[dict(id=x[0],flags=x[1],client_data=list(x[2]),name=x[3].decode()) for x in [raw.id.get_create_plist().get_filter(i) for i in range(raw.id.get_create_plist().get_nfilters())]]
    chunks=[]
    for i in range(raw.id.get_num_chunks()):
     info=raw.id.get_chunk_info(i);mask,data=raw.id.read_direct_chunk(info.chunk_offset)
     chunks.append(dict(coordinate=list(info.chunk_offset),offset=info.byte_offset,bytes=len(data),filter_mask=mask,sha256=hashlib.sha256(data).hexdigest()))
    entry['source_chunks']=chunks
  model=projection(case);write(directory/'projection.json',model)
  facts=dict(schema_version=SCHEMA,case=case,source=dict(file='recording.raw.h5',size=source.stat().st_size,sha256=sha(source)),
   projection=dict(file='projection.json',size=(directory/'projection.json').stat().st_size,sha256=sha(directory/'projection.json'),original_typed=typed(model)),
   stores=stores,model=model_contract(model),
   expected_interface=dict(device=dict(name='DeviceEcephys',description="Recorded using Maxwell version '20190530'."),
    electrode_group=dict(name='0',description='no description',location='unknown',device='DeviceEcephys'),
    electrical_series=dict(name='ElectricalSeries',description='Acquisition traces for the ElectricalSeries.',unit='volts',
     rate_hz=RATE,starting_time_hex=0.0.hex(),offset_hex=0.0.hex(),resolution_hex=(-1.0).hex(),comments='no comments',timestamps=None,channel_conversion=None),
    metadata_Data_payloads=12,all_Data_payloads_including_signal_region=13,canonical_containers_including_Data=30,
    library_defaults_must_match_pinned_interface_and_schema=True),expected_core=typed(dict(identifier=model['dataset_uuid']+':'+model['experiment_name']+':'+model['unit'],
    session_id=model['context']['session_id'],session_description=model['context']['session_description'],session_start_time=model['context']['session_start_time'],
    experimenter=[x['name'] for x in model['people'].values()],**model['common'])))
  write(directory/'facts.json',facts)
  uri=uri_prefix+case+'/recording.raw.h5';puri=uri_prefix+case+'/projection.json'
  unknown=dict(integer=7,floating=7.0,signed_zero=-0.0,large_integer=2**80+17,negative_integer=-(2**80+17),bool=False,null=None,unicode='μV',keep=[True,3,2.5])
  record=dict(input_uri=uri,data_format='maxone' if case=='maxone' else 'maxtwo',
   nwb_metadata={('recording' if case=='maxone' else 'well002'):dict(uri=puri,sha256=facts['projection']['sha256'],size=facts['projection']['size'],etag='sha256-'+facts['projection']['sha256'])},
   uploaded_inventory={'preserve':['original/path/recording.raw.h5']},unknown_typed=unknown)
  if case!='maxone':record.update(wells=['well000','well002'],well_filter=['well002'])
  recordings.append(record);bindings.append(dict(input_uri=uri,unit_name='recording' if case=='maxone' else 'well002',metadata=dict(unit_id=case,inventory='retain-'+case,unknown_typed=unknown)))
  summaries.append(dict(case=case,source_sha256=facts['source']['sha256'],projection_sha256=facts['projection']['sha256'],facts_sha256=sha(directory/'facts.json')))
 write(root/'recordings.json',recordings);write(root/'bindings.json',bindings)
 write(root/'transport-facts.json',dict(recordings=typed(recordings),bindings=typed(bindings)))
 write(root/'input-set.json',dict(schema_version=SCHEMA,uri_prefix=uri_prefix,cases=summaries,produced_nwb_outputs_used=False,complete_checker_proven=False))
 return summaries

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path);parser.add_argument('--uri-prefix',default='file:///inputs/')
 args=parser.parse_args();print(json.dumps(prepare(args.output,args.uri_prefix),indent=2))
