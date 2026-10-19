#!/usr/bin/env python3
"""Observe an original CLI's exception chain while preserving its exit behavior.

This explicit test entry point uses only stdlib before the reviewed script starts.
runpy supplies its normal __main__/__file__/argv; no scientific function, argument,
exception or exit handler is replaced. Docker remains the authority for the exit.
"""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import json
import os
import runpy
import sys
import time
import traceback


def sha(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def inventory(folder):
    folder=Path(folder)
    return {str(path.relative_to(folder)):dict(bytes=path.stat().st_size,sha256=sha(path))
            for path in sorted(folder.rglob('*')) if path.is_file() and not path.is_symlink()}


def main():
    manifest_path=Path(sys.argv[1]);manifest=json.loads(manifest_path.read_text())
    argv=manifest['argv'];target=Path(argv[0]);cwd=Path(manifest['cwd'])
    assert target.is_file() and sha(target)==manifest['program_sha256']
    assert all(type(value) is str for value in argv)
    output=Path(manifest['journal']);output.parent.mkdir(parents=True,exist_ok=True)
    assert not output.exists()
    plugin_paths=[Path('/opt/maxwell-hdf5-plugin/libcompression.so'),Path.home()/'hdf5_plugin_path_maxwell/libcompression.so']
    record=dict(label=manifest['label'],manifest_sha256=sha(manifest_path),argv=argv,
        actual_process_argv=list(sys.argv),program_sha256=sha(target),pid=os.getpid(),uid=os.getuid(),gid=os.getgid(),
        started_at_utc=datetime.now(timezone.utc).isoformat(),observer_sha256=sha(__file__),
        xdg_cache_home=os.environ.get('XDG_CACHE_HOME'),hdf5_plugin_path=os.environ.get('HDF5_PLUGIN_PATH'),
        plugin_files={str(path):dict(exists=path.is_file(),sha256=sha(path) if path.is_file() else None) for path in plugin_paths},
        publication_before=inventory(manifest['publication']),
        protected_before={path:sha(path) for path in manifest['protected_files']},
        original_cli_run_with_runpy=True,scientific_functions_arguments_or_exit_behavior_replaced=False)
    # Initialize the journal before any scientific import or guarded operation.
    output.write_text(json.dumps(dict(record,journal_initialized=True),indent=2)+'\n')
    started=time.monotonic();os.chdir(cwd);sys.argv=list(argv)
    handle=None
    if manifest.get('stdin_file'):
        handle=Path(manifest['stdin_file']).open(encoding='utf-8');sys.stdin=handle
        record['stdin_sha256']=sha(manifest['stdin_file'])
    try:
        runpy.run_path(str(target),run_name='__main__')
        record.update(normal_return=True,observed_exit=0,exceptions=[])
    except BaseException as error:
        chain=[];seen=set();current=error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(dict(type=type(current).__name__,module=type(current).__module__,message=str(current),
                frames=[dict(filename=frame.filename,function=frame.name,line=frame.lineno) for frame in traceback.extract_tb(current.__traceback__)],
                suppress_context=bool(current.__suppress_context__)))
            current=current.__cause__ or current.__context__
        observed=error.code if isinstance(error,SystemExit) and type(error.code) is int else 1
        record.update(normal_return=False,observed_exit=observed,exceptions=chain)
        raise
    finally:
        if handle:handle.close()
        record.update(duration_s=time.monotonic()-started,ended_at_utc=datetime.now(timezone.utc).isoformat(),
            publication_after=inventory(manifest['publication']),
            protected_after={path:sha(path) for path in manifest['protected_files']},
            program_sha256_after=sha(target),journal_initialized=True)
        output.write_text(json.dumps(record,indent=2)+'\n')


if __name__=='__main__':main()
