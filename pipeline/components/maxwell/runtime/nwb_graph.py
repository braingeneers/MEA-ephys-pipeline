"""Read-only admission for an NWB graph that will be exported or appended."""


def require_writable_graph(reader):
    """Require every concrete group builder to have one actual parent.

    References and LinkBuilders may point at a group from several places. They
    do not re-parent it. Old expanded-reference repacks can instead index one
    concrete GroupBuilder beneath two parents, which HDMF reads but cannot write.
    Names (including old core root aliases) do not determine this invariant.
    """
    root = reader.read_builder()
    seen, pending = {}, [(root, '/')]
    while pending:
        parent, path = pending.pop()
        if id(parent) in seen:
            raise ValueError('NWB write blocked: a concrete group has multiple builder locations '
                f'({seen[id(parent)]}, {path}). Create a fresh conversion from the verified '
                'original/recovery, or an explicitly validated metadata-refresh copy. '
                'Keep the stored NWB and its evidence unchanged.')
        seen[id(parent)] = path
        for name, child in parent.groups.items():
            child_path = path.rstrip('/') + '/' + name
            if child.parent is not parent:
                raise ValueError('NWB write blocked: ambiguous group parentage at ' + child_path +
                    '. Create a fresh conversion from the verified original/recovery, or an '
                    'explicitly validated metadata-refresh copy. Keep the stored NWB and '
                    'its evidence unchanged.')
            pending.append((child, child_path))
    return {'concrete_groups_checked': len(seen), 'unique_builder_parentage': True}


def require_writable_hdf5(path):
    """Inspect cached builders without changing source bytes or registering facts."""
    from pynwb import NWBHDF5IO

    with NWBHDF5IO(str(path), 'r', load_namespaces=True) as reader:
        return require_writable_graph(reader)
