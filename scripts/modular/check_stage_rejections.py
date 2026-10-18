#!/usr/bin/env python3
"""Reject resealed, scientifically incomplete graph-probe bundles.

Each mutation has a valid ordinary file inventory and manifest digest, so these
checks exercise stage-owned evidence policy rather than only generic checksums.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "pipeline/helpers"))
import portable_bundle as portable
import stage_bundle as stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    emitted=json.loads((args.graph/"emitted.json").read_text())
    base=Path(emitted[0]["path"]).resolve()
    no_spikes=stages.classifier(SOURCE/"capsules/kilosort4/code/outcomes.py")
    record=stages.verify(base,"exported",emitted[0]["metadata"]["unit_id"],no_spikes)
    rid=record["recording_ids"][0]
    unit=record["unit_id"]
    results={}
    for name,kind,expected_unit in (("wrong_kind","analyzed",unit),("wrong_unit","exported","other")):
        try:
            stages.verify(base,kind,expected_unit,no_spikes)
        except portable.BundleError as error:
            results[name]=str(error)
        else:
            raise AssertionError(name)

    def json_edit(root,path,change):
        target=root/"data"/path
        value=json.loads(target.read_text())
        change(value)
        target.write_text(json.dumps(value))

    def hide(root,path):
        (root/"data"/path).rename(args.output/(root.name+"-removed"))

    changes={
        "missing_history":lambda root,r:r["metadata"].update(completed_stages=["exported"]),
        "wrong_receipt":lambda root,r:r["metadata"]["outcomes"][rid].update(sorted_spikes=99),
        "unknown_sorter_error":lambda root,r:json_edit(root,f"spikesorted_{rid}/spikeinterface_log.json",
            lambda log:log.update(error_trace=["ValueError: unrelated failure"])),
        "incomplete_zero_log":lambda root,r:json_edit(root,f"spikesorted_{rid}/spikeinterface_log.json",
            lambda log:log.update(runtime_trace=["st0 shape: (0, 6)"])),
        "wrong_zero_frames":lambda root,r:json_edit(root,f"spikesorted_{rid}/sorting_status.json",
            lambda status:status.update(frames=[1])),
        "missing_zero_evidence":lambda root,r:hide(root,f"spikesorted_{rid}/sorting_status.json"),
        "missing_report":lambda root,r:hide(root,f"reports/{rid}"),
        "wrong_report_status":lambda root,r:json_edit(root,f"reports/{rid}/report_summary.json",
            lambda summary:summary.update(status="ok")),
        "wrong_burst_status":lambda root,r:json_edit(root,f"bursts/{rid}/network_results.json",
            lambda result:result.update(status="ok")),
        "missing_collected_family":lambda root,r:hide(root,"collected/curated"),
        "missing_export":lambda root,r:hide(root,"derived-nwb"),
        "unexpected_report":lambda root,r:(root/'data/reports/unexpected-recording').mkdir(),
    }
    for name,change in changes.items():
        root=args.output/name
        shutil.copytree(base,root)
        value=deepcopy(record)
        change(root,value)
        value["files"]=portable.inventory(root/"data")
        value["bytes"]=sum(entry["bytes"] for entry in value["files"].values())
        value.pop("content_sha256")
        value["content_sha256"]=hashlib.sha256(portable.encoded(value)).hexdigest()
        (root/"bundle.json").write_bytes(portable.encoded(value)+b"\n")
        portable.verify(root)
        try:
            stages.verify(root,"exported",unit,no_spikes)
        except (portable.BundleError,FileNotFoundError) as error:
            results[name]=str(error)
        else:
            raise AssertionError(name+" accepted")
    # Preserve a reported-kind malformed input for an actual FINAL_EXPORT launch
    # rejection. It must fail verification before either NWB process is scheduled.
    reported=args.graph/'published'/emitted[0]['alias']/'reporting/bundles'/(unit+'.reported')
    root=args.output/'incomplete-reported'
    shutil.copytree(reported,root)
    hide(root,f'reports/{rid}')
    value=stages.read_record(root)
    value['files']=portable.inventory(root/'data')
    value['bytes']=sum(entry['bytes'] for entry in value['files'].values())
    value.pop('content_sha256')
    value['content_sha256']=hashlib.sha256(portable.encoded(value)).hexdigest()
    (root/'bundle.json').write_bytes(portable.encoded(value)+b'\n')
    portable.verify(root)
    try:
        stages.verify(root,'reported',unit,no_spikes)
    except FileNotFoundError as error:
        results['missing_reported_input']=str(error)
    else:
        raise AssertionError('Incomplete reported input accepted')
    prior=stages.read_record(reported)
    try:
        stages.build({'schema_version':1,'phase':'exported','unit_id':unit,'unit_meta':{'unit_id':unit},
                      'context':prior['metadata']['context'],'pieces':[],'previous':0,'input_count':1},
                     [reported],args.output/'metadata-changed',no_spikes)
    except portable.BundleError as error:
        assert 'unit metadata' in str(error)
        results['unit_metadata_loss']=str(error)
    else:
        raise AssertionError('Dropped predecessor metadata accepted')
    # Missing nullable keys and boolean/integer coercion must not look unchanged.
    for name,old_meta,new_meta in [('nullable_metadata_loss',{'nullable':None},{}),
                                  ('typed_metadata_change',{'typed':1},{'typed':True})]:
        candidate=args.output/(name+'-source')
        shutil.copytree(reported,candidate)
        value=stages.read_record(candidate)
        value['metadata']['unit_meta'].update(old_meta)
        value.pop('content_sha256')
        value['content_sha256']=hashlib.sha256(portable.encoded(value)).hexdigest()
        (candidate/'bundle.json').write_bytes(portable.encoded(value)+b'\n')
        try:
            stages.build({'schema_version':1,'phase':'exported','unit_id':unit,
                          'unit_meta':dict(prior['metadata']['unit_meta'],**new_meta),
                          'context':prior['metadata']['context'],'pieces':[],'previous':0,'input_count':1},
                         [candidate],args.output/name,no_spikes)
        except portable.BundleError as error:
            assert 'unit metadata' in str(error)
            results[name]=str(error)
        else:
            raise AssertionError(name+' accepted')
    result={"passed":True,"scientific_capsules_stubbed":True,"rejections":results,
            "resealed_inventory_passed_before_policy":len(changes)}
    (args.output/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
