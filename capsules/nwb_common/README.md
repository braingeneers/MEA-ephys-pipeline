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
