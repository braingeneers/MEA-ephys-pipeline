#!/usr/bin/env python3
"""Exercise inactive named stages and retained input guards in real Nextflow.

Host orchestration only. Both engines execute in a network-isolated Docker image;
no task, scientific capsule, S3 operation or NERSC/Shifter execution is permitted.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--driver-image', default='nextflow/nextflow@sha256:83bbf3dd9e84ecd4d53a86620d08fe0259bca00a8f59f6f31664eb90033f460f')
    parser.add_argument('--legacy-runtime', type=Path, help='Verified Nextflow23 all launcher and Java17 cache')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    assert '@sha256:' in args.driver_image, 'Use an immutable driver image'
    legacy_sha = None
    if args.legacy_runtime:
        args.legacy_runtime = args.legacy_runtime.resolve()
        legacy_sha = hashlib.sha256((args.legacy_runtime/'nextflow-23.08.0-edge-all').read_bytes()).hexdigest()
        assert legacy_sha == '4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2'
        assert '17.0.16' in (args.legacy_runtime/'jdk-17.0.16+8-jre/release').read_text()
    imports = """nextflow.enable.dsl=2
include { MEA_PREPROCESSING as PREP; MEA_KILOSORT4 as SORT; MEA_ANALYSIS as ANALYZE;
 MEA_REPORTING as REPORT; MEA_FINAL_EXPORT as EXPORT; stageInputs } from '/mea/pipeline/stages'
def localContext = [output_uri:'/evidence/products',workflow_version:'probe',run_id:'probe',step_id:'empty']
"""
    conversion = """include { MEA_MAXWELL_CONVERSION as CONVERT; conversionOwnerVersion; conversionVersion } from '/mea/pipeline/conversion'
include { PREPARE_MAXWELL_SELECTION as RAW; MAXWELL_TO_NWB as NATIVE } from '/mea/pipeline/components/maxwell/component'
def conversionContext = [output_uri:'/evidence/products',native_output_uri:'s3://test/never-written/',
 workflow_version:conversionOwnerVersion(),run_id:'probe',step_id:'empty',unit_bindings:{ rows -> [] },
 runtime:[public_reader_endpoint:'https://example.invalid']]
def nativeContext = [output_uri:'s3://test/never-written/',output_base_uri:'s3://test/never-written/',
 collection_uri:'s3://test/never-written/derived/nwb/',workflow_version:conversionVersion('/mea/pipeline/components/maxwell/nextflow.config'),
 run_id:'probe',step_id:'empty',runtime:[public_reader_endpoint:'https://example.invalid']]
"""
    empty = """ PREP(channel.empty(),[sorter:'kilosort4'],localContext)
 SORT(channel.empty(),[sorter:'kilosort4'],localContext)
 ANALYZE(channel.empty(),[sorter:'kilosort4'],localContext)
 REPORT(channel.empty(),[sorter:'kilosort4'],localContext)
 EXPORT(channel.empty(),[sorter:'kilosort4'],localContext)
 PREP.out.units.toList().view { 'PORT|prepared|'+it.size() }
 SORT.out.units.toList().view { 'PORT|sorted|'+it.size() }
 ANALYZE.out.units.toList().view { 'PORT|analyzed|'+it.size() }
 REPORT.out.units.toList().view { 'PORT|reported|'+it.size() }
 EXPORT.out.units.toList().view { 'PORT|exported|'+it.size() }
"""
    native_empty = """ CONVERT(channel.empty(),[:],conversionContext)
 RAW(channel.empty(),[:],nativeContext)
 NATIVE(channel.empty(),[:],nativeContext)
 CONVERT.out.units.toList().view { 'PORT|conversion.units|'+it.size() }
 CONVERT.out.artifacts.toList().view { 'PORT|conversion.artifacts|'+it.size() }
 CONVERT.out.completion.toList().view { 'PORT|conversion.completion|'+it.size() }
 RAW.out.artifacts.toList().view { 'PORT|native.raw|'+it.size() }
 NATIVE.out.artifacts.toList().view { 'PORT|native.conversion|'+it.size() }
"""
    cases = [
        ('empty-stages', empty, True, ['PORT|'+name+'|0' for name in ('prepared','sorted','analyzed','reported','exported')]),
        ('populated-unit-validation', """ stageInputs(channel.of(tuple([unit_id:'one',unknown:'keep'],file('/evidence/unit')),
 tuple([unit_id:'two',unknown:'also-keep'],file('/evidence/unit')))).toList().view { rows ->
  assert rows.size()==2 && rows[0][0].unknown=='keep' && rows[1][0].unknown=='also-keep'
  'VALID|two-distinct-units'
 }
""", True, ['VALID|two-distinct-units']),
        ('duplicate-units', """ stageInputs(channel.of(tuple([unit_id:'duplicate'],file('/evidence/unit')),
 tuple([unit_id:'duplicate'],file('/evidence/unit')))).view()
""", False, ['Duplicate unit identities']),
        ('malformed-unit', " stageInputs(channel.of(tuple([unknown:'invalid'],file('/evidence/unit')))).view()\n",
         False, ['Every unit needs a stable path-safe']),
    ]
    modern = [
        ('empty-conversion', native_empty, True, ['PORT|'+name+'|0' for name in
            ('conversion.units','conversion.artifacts','conversion.completion','native.raw','native.conversion')]),
        ('explicit-empty-selection', ' CONVERT(channel.value([]),[:],conversionContext)\n',
         False, ['Expected one nonempty complete recordings selection']),
        ('multiple-conversion-selections', ' CONVERT(channel.of([],[]),[:],conversionContext)\n',
         False, ['Expected one complete recordings selection']),
        ('multiple-native-selections', ' RAW(channel.of([],[]),[:],nativeContext)\n',
         False, ['Expected one complete recordings selection']),
        ('multiple-native-artifacts', " NATIVE(channel.of('s3://test/a','s3://test/b'),[:],nativeContext)\n",
         False, ['Expected one complete artifact envelope']),
    ]
    results = []
    for engine in ['26'] + (['23'] if args.legacy_runtime else []):
        for label, body, success, expected in cases + (modern if engine=='26' else []):
            folder = root/(engine+'-'+label)
            folder.mkdir(); (folder/'products').mkdir(); (folder/'unit').mkdir()
            (folder/'main.nf').write_text(imports+(conversion if engine=='26' else '')+'\nworkflow {\n'+body+'}\n')
            name = 'mea-empty-'+uuid.uuid4().hex
            command = ['docker','run','--rm','--name',name,'--network','none','--cpus','2','--memory','4g',
                '-e','NXF_OFFLINE=true','-v',str(source)+':/mea:ro','-v',str(folder)+':/evidence','-w','/evidence']
            if engine=='23':
                command += ['-v',str(args.legacy_runtime)+':/legacy:ro',
                    '-e','JAVA_HOME=/legacy/jdk-17.0.16+8-jre','-e','NXF_HOME=/evidence/home',
                    '-e','NXF_DIST=/evidence/framework','-e','NXF_PLUGINS_DIR=/evidence/plugins',
                    '-e','NXF_VER=23.08.0-edge']
            else:
                command += ['-e','NXF_SYNTAX_PARSER=v1']
            command += [args.driver_image,'/legacy/nextflow-23.08.0-edge-all' if engine=='23' else 'nextflow',
                '-log','/evidence/nextflow.log','run','/evidence/main.nf','-work-dir','/evidence/work',
                '-ansi-log','false','-with-trace','/evidence/trace.tsv']
            if engine=='26':
                command += ['-lib','/mea/pipeline/components/maxwell/lib']
            (folder/'command.json').write_text(json.dumps(command,indent=2)+'\n')
            try:
                with (folder/'console.log').open('w') as stream:
                    completed = subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,timeout=150)
            finally:
                if subprocess.run(['docker','inspect',name],capture_output=True).returncode==0:
                    subprocess.run(['docker','stop','--time','2',name],capture_output=True,check=True)
            output = (folder/'console.log').read_text()
            assert (completed.returncode==0)==success,(engine,label,output[-5000:])
            assert ('version '+('23.08.0' if engine=='23' else '26.04.6')) in output,(engine,label,output[-5000:])
            for needle in expected:
                assert needle in output,(engine,label,needle,output[-5000:])
            tasks = []
            if (folder/'trace.tsv').exists():
                with (folder/'trace.tsv').open() as stream:
                    tasks = list(csv.DictReader(stream,delimiter='\t'))
            assert not tasks,(engine,label,tasks)
            results.append(dict(engine=engine,case=label,exit_code=completed.returncode,tasks=0))
            print(json.dumps(results[-1]),flush=True)
    report = dict(passed=True,checks=results,driver_image=args.driver_image,legacy_launcher_sha256=legacy_sha,
                  scientific_execution=False,s3_operations=False,nersc_shifter_executed=False)
    (root/'result.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    main()
