"""Observe real test task invocations without replacing scientific functions.

Loaded only when the test harness explicitly adds this directory to PYTHONPATH.
Normal-exit callbacks do not prove exit zero: bind them to the owning worker's
actual child exit and task outcome. Observer files live outside guarded scratch.
"""
import atexit
import base64
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import time
import uuid


def identity(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return dict(bytes=path.stat().st_size, sha256=value.hexdigest())


def observe():
    destination = os.environ.get('KS4_TASK_OBSERVER_DIR')
    if not destination or not sys.argv:
        return
    program = Path(sys.argv[0])
    tools = Path('/opt/ephys-modular/maxwell/scripts')
    names = {path.name for path in tools.glob('*.py')}
    names.update(('native_task.py', 'maxwell_bridge.py', 'verify_outputs.py'))
    if program.name not in names or not program.is_file():
        return
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    key = str(os.getpid()) + '-' + uuid.uuid4().hex
    started = time.monotonic()

    def inputs():
        files = {}
        for arg in sys.argv[1:]:
            path = Path(arg)
            if path.suffix in ('.nwb', '.h5', '.json') and path.is_file():
                details = identity(path)
                if path.suffix == '.json':
                    with path.open('rb') as stream:
                        content = stream.read(32 * 1024 * 1024 + 1)
                    if len(content) <= 32 * 1024 * 1024:
                        assert hashlib.sha256(content).hexdigest() == details['sha256']
                        details['content_b64'] = base64.b64encode(content).decode('ascii')
                    else:
                        details['json_content_exceeds_observation_limit'] = True
                files[str(path.absolute())] = details
        return files

    record = dict(argv=list(sys.argv), executable=sys.executable, cwd=str(Path.cwd()),
                  pid=os.getpid(), uid=os.getuid(), gid=os.getgid(),
                  hostname=Path('/etc/hostname').read_text().strip(),
                  program=str(program.absolute()), program_identity=identity(program),
                  started_at_utc=datetime.now(timezone.utc).isoformat(),
                  files_before=inputs(), xdg_cache_home=os.environ.get('XDG_CACHE_HOME'),
                  observer_sha256=identity(Path(__file__))['sha256'],
                  observation_only=True, scientific_functions_or_exit_behavior_replaced=False)
    if program.name in ('native_task.py', 'maxwell_bridge.py'):
        record['installed_tools'] = {path.name:identity(path) for path in sorted(tools.glob('*.py'))}
        record['distributions'] = sorted([dict(name=d.metadata['Name'], version=d.version)
                                          for d in metadata.distributions()], key=lambda row:row['name'].lower())
        record['plugin'] = identity(Path('/opt/maxwell-hdf5-plugin/libcompression.so'))
    with (output / (key + '.start.json')).open('x') as stream:
        stream.write(json.dumps(record, indent=2) + '\n')

    def finish():
        final = dict(pid=os.getpid(), program=str(program.absolute()),
                     ended_at_utc=datetime.now(timezone.utc).isoformat(),
                     duration_s=time.monotonic() - started, files_after=inputs(),
                     program_identity_after=identity(program), atexit_reached=True,
                     actual_child_exit_must_be_read_from_owning_worker=True)
        with (output / (key + '.end.json')).open('x') as stream:
            stream.write(json.dumps(final, indent=2) + '\n')
    atexit.register(finish)


observe()
