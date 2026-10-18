# Source NWB metadata

`code/source_nwb.py` is shared by the two maintained NWB capsules. Nextflow stages
it as an explicit process input and copies it beside each capsule's runtime code.
For a manual capsule invocation, copy the helper into that code directory or put
this `code/` directory on `PYTHONPATH`.

Resolve headers only from executable NWB recording kwargs and their declared
serialization base. Ignore historical annotations. One session must have one
unambiguous source header; non-NWB recordings retain the existing metadata path.
Importing a header preserves its subject, session and other authored metadata.
The exporter's existing raw/LFP options select the electrophysiology signals.

NeuroConv identifies electrodes by channel name **and** group name. Preserve both
and the source device binding when appending LFP or units. Repeated channel names
require an explicit group identity; never invent an electrode row to resolve an
ambiguous match. Units spanning multiple devices use their template peak channel
to retain the corresponding hardware name.

Run `scripts/modular/check_nwb_source_metadata.py` in the pinned NWB worker against
real raw/units exports and the prior derived NWB. It also checks malformed source
graphs and repeated channel names across two devices. The original isolated NWB
validator remains a separate schema/data check.

Both capsules open source headers through `open_source_nwb`. It inspects the
source's declared core and cached namespace versions before merging schemas into
a private type map. Older supported core files remain readable; a newer core or
a different `ndx-cell-culture` version fails explicitly. Modeled files require the
pinned extension package. HDF5 and Zarr use their own backend namespace loaders.
Rejected files cannot replace another invocation's registered schema map.

The [NWB I/O candidate](../../containers/nwb-io/README.md) supplies the compatible
reader stack. Its complete typed-read checks do not establish modeled graph
preservation through export/append or acceptance of the full scientific producers.
