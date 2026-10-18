# Upstream capsule

Vendored from https://github.com/Varda006/aind-ecephys-nwb at `6bfcc77ae2f3ef504b487d003f7c4a20639dfbc4`.
Only runtime code is included; the upstream license is retained.

Local changes:

- Only convert unsigned integer recordings before filtering. Signed and
  floating-point inputs retain their original representation.
- Preserve the final fractional second of LFP data with the pinned resampler.
- Select LFP channels after filtering and binary materialization. Reading
  contiguous channels avoids large HDF5 selections for compressed NWB input;
  the per-channel filters and selected electrode IDs are unchanged. The
  temporary LFP binary contains all channels before the final selection.
- Resolve a staged source NWB from the session's executable job recording graph,
  including nested extractors. Import its authored metadata instead of falling
  back to a mock subject and export wall clock. The existing raw/LFP branches still
  own signal selection, and explicit flat NWB inputs retain their prior behavior.
  Multiple source headers within one session are rejected as ambiguous.
- Preserve existing electrode channel/group identities and device metadata when
  adding derived signals, rather than appending duplicate electrode rows. The
  explicitly staged shared helper is documented in `../nwb_common/README.md`.
