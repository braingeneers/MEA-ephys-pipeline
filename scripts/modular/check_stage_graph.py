#!/usr/bin/env python3
"""Execute real named-stage/process graph with test-only scientific capsules.

Run inside the pinned Nextflow driver with the Docker socket/CLI and same absolute
source/output paths mounted. It creates a separate snapshot; production is read-only.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-runtime", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    snapshot = root / "source"
    for folder in ("pipeline","capsules","scripts/nrp","containers/analysis"):
        shutil.copytree(source / folder, snapshot / folder)
    images = json.loads((source / "scripts/nrp/images.json").read_text())
    overrides = {role: item["local_image_id"] for role, item in
                 json.loads((source / "containers/runtime-images.json").read_text())["images"].items()}
    fixture_script = source / "scripts/modular/stage_probe_capsule.py"
    subprocess.run(["docker","run","--rm","--cpus","1","--memory","2g","--user","1000:1000",
                    "-v",f"{source}:{source}:ro","-v",f"{root}:{root}","-e","HOME=/tmp",
                    "-e","OMP_NUM_THREADS=1","-e","OPENBLAS_NUM_THREADS=1",images["nwb"],
                    "python",str(fixture_script),"--fixture",str(root / "data")], check=True)
    roles = ("job_dispatch","preprocessing","kilosort4","postprocessing","curation","visualization","nwb_ecephys","nwb_units","results_collector")
    for role in roles:
        code = snapshot / "capsules" / role / "code"
        code.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_script, code / "stage_probe_capsule.py")
        (code / "run").write_text(f'#!/usr/bin/env bash\nexec python stage_probe_capsule.py {role} "$@"\n')
        (code / "get_max_recording_duration_min.py").write_text("print('0.041666666666666664')\n")
    resources = {name:{"cpus":1,"memory":"2 GB","time":"10 min"} for name in
                 ("job_dispatch","preprocessing","spikesort_kilosort4","postprocessing","curation","visualization",
                  "results_collector","report_generation","burst_detection","nwb_ecephys","nwb_units","bundle_stage")}
    # Separate aliases have the same unit names and source basenames. Distinct
    # settings and context roots must survive every nested process invocation.
    for alias in ("alpha","beta"):
        scoped_resources = {name:dict(value, cpus=1 if alias == "alpha" else 2,
                                      memory="2 GB" if alias == "alpha" else "3 GB")
                            for name,value in resources.items()}
        (root / (alias + ".json")).write_text(json.dumps({"ecephys_path":str(root / "data/slow"),
            "results_path":str(root / "published" / alias), "torch_device":"cpu","n_jobs":1,
            "git_repo_prefix":"https://github.com/AllenNeuralDynamics/aind-",
            "process_resources":scoped_resources, "container_images":{} if alias == "alpha" else overrides,
            "params_file":str(root / (alias + "-science.json"))}))
        (root / (alias + "-science.json")).write_text(json.dumps({"spikesorting":{"sorter":"kilosort4",
            "kilosort4":{"sorter":{"torch_device":"cpu","probe_marker":alias}}}}))
    (root / "component.nf").write_text(f'''nextflow.enable.dsl=2
include {{ MEA_PREPROCESSING; MEA_KILOSORT4; MEA_ANALYSIS; MEA_REPORTING; MEA_FINAL_EXPORT }} from '{snapshot}/pipeline/stages'
workflow PROBE {{
 take: units; settings; context
 main:
 MEA_PREPROCESSING(units,settings,context+[output_uri:context.output_uri+'/preprocessing'])
 MEA_KILOSORT4(MEA_PREPROCESSING.out.units,settings,context+[output_uri:context.output_uri+'/sorting'])
 MEA_ANALYSIS(MEA_KILOSORT4.out.units,settings,context+[output_uri:context.output_uri+'/analysis'])
 MEA_REPORTING(MEA_ANALYSIS.out.units,settings,context+[output_uri:context.output_uri+'/reporting'])
 MEA_FINAL_EXPORT(MEA_REPORTING.out.units,settings,context+[output_uri:context.output_uri+'/export'])
 emit: units=MEA_FINAL_EXPORT.out.units
}}
''')
    (root / "main.nf").write_text(f'''nextflow.enable.dsl=2
include {{ loadLaunchSettings }} from '{snapshot}/pipeline/settings'
include {{ PROBE as ALPHA; PROBE as BETA }} from './component'
a=loadLaunchSettings(new groovy.json.JsonSlurper().parse(new File('{root}/alpha.json')),file('{snapshot}/pipeline'))
b=loadLaunchSettings(new groovy.json.JsonSlurper().parse(new File('{root}/beta.json')),file('{snapshot}/pipeline'))
workflow {{
 rows=channel.of(tuple([unit_id:'slow',inventory:'keep-slow'],file('{root}/data/slow')),tuple([unit_id:'fast',inventory:'keep-fast'],file('{root}/data/fast')))
 ALPHA(rows,a,[output_uri:'{root}/published/alpha',workflow_version:'0.2.0',step_id:'alpha'])
 BETA(rows,b,[output_uri:'{root}/published/beta',workflow_version:'0.2.0',step_id:'beta'])
 ALPHA.out.units.map{{meta,path -> [alias:'alpha',metadata:meta,path:path.toString()]}}
 .mix(BETA.out.units.map{{meta,path -> [alias:'beta',metadata:meta,path:path.toString()]}})
 .collect().subscribe{{ rows -> file('emitted.json').text=groovy.json.JsonOutput.toJson(rows) }}
}}
''')
    overlay = root / "probe.config"
    overlay.write_text("executor.queueSize=8\nexecutor.pollInterval='1 sec'\n"
        + "docker.runOptions='--user 1000:1000 --network none -e HOME=/tmp -e MPLBACKEND=Agg'\n")
    command = ["nextflow"]
    env = os.environ.copy()
    if args.legacy_runtime:
        cache = args.legacy_runtime
        executable = cache / "nextflow-23.08.0-edge-all"
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == "4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2"
        command = [str(executable)]
        env.update(JAVA_HOME=str(cache / "jdk-17.0.16+8-jre"),NXF_HOME=str(cache / "home"),
                   NXF_DIST=str(cache / "framework"),NXF_PLUGINS_DIR=str(cache / "plugins"),NXF_VER="23.08.0-edge")
        env.pop("NXF_SYNTAX_PARSER",None)
    command += ["-C",str(snapshot / "pipeline/nextflow_stage_docker.config")+","+str(overlay),
                "run",str(root / "main.nf"),"-ansi-log","false","-with-trace",str(root / "trace.tsv")]
    (root / "command.json").write_text(json.dumps(command,indent=2)+"\n")
    with (root / "run.log").open("w") as log:
        subprocess.run(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1200)
    emitted = json.loads((root / "emitted.json").read_text())
    assert len(emitted)==4
    for row in emitted:
        unit,alias = row["metadata"]["unit_id"],row["alias"]
        assert row["metadata"]["inventory"] == "keep-"+unit
        record = json.loads((Path(row["path"])/"bundle.json").read_text())
        assert record["metadata"]["context"]["step_id"]==alias
        assert record["metadata"]["context"]["workflow_version"]=="0.2.0"
        assert record["metadata"]["completed_stages"]==["dispatch","prepared","sorted","analyzed","reported","exported"]
        outcome = record["metadata"]["outcomes"]["block0_acquisition-ElectricalSeriesRaw_recording1"]
        assert outcome["status"]=="no_spikes_detected" and outcome["report_status"]=="no_detected_units" and outcome["burst_status"]=="no_spikes"
        assert outcome["frames"]==[60000 if unit=="slow" else 75000]
        sorter = json.loads((Path(row["path"])/"data/probe_kilosort4.json").read_text())
        params = json.loads(sorter["argv"][sorter["argv"].index("--params")+1])
        assert params["sorter"]["probe_marker"]==alias and params["sorter"]["torch_device"]=="cpu"
        assert set(sorter["threads"].values())=={"1" if alias=="alpha" else "2"}
        for provenance in record["metadata"]["stage_provenance"]:
            for process in provenance["processes"]:
                name, execution = process["name"], process["execution"]
                role = "ks4" if name=="spikesort_kilosort4" else "nwb" if name in ("nwb_ecephys","nwb_units") else "analysis" if name=="report_generation" else "base"
                assert execution["container"]==(images[role] if alias=="alpha" else overrides[role])
                assert execution["cpus"]==(1 if alias=="alpha" else 2)
                assert execution["memory"]==("2 GB" if alias=="alpha" else "3 GB")
        assert (root/"published"/alias/"export/bundles"/(unit+".exported")/"bundle.json").is_file()
    with (root/"trace.tsv").open() as stream:
        rows=list(csv.DictReader(stream,delimiter="\t"))
    assert len(rows)==84 and all(row["status"]=="COMPLETED" and row["exit"]=="0" for row in rows)
    result={"passed":True,"tasks":len(rows),"legacy_runtime":bool(args.legacy_runtime),
            "scientific_capsules_stubbed":True,"real_report_and_burst_capsules":True,
            "real_bundle_verifiers":True,"alias_unit_settings_publication_isolation":True,"emitted":emitted}
    (root/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps({key:value for key,value in result.items() if key!="emitted"},indent=2))


if __name__=="__main__":
    main()
