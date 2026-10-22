nextflow.enable.dsl = 2

// The complete rendered recorder is a value input, so edits invalidate cached
// producers even in a Gitless local/ConfigMap launch. Only executable metadata
// is normalized; scientific payloads remain unchanged and fully verified later.
def producerLocationOptions(String role, Map serializationSources=[:]) {
    if (!(role in ['job_dispatch', 'preprocessing', 'spikesort_kilosort4',
                   'postprocessing', 'curation', 'results_collector'])) return [:]
    if (serializationSources.keySet() != ['portable_bundle', 'producer_serializations'].toSet()
        || serializationSources.values().any { !(it instanceof String) || !it })
        throw new IllegalArgumentException('Producer normalization requires its maintained helper sources')
    def modules = groovy.json.JsonOutput.toJson(serializationSources)
        .getBytes('UTF-8').encodeBase64().toString()
    def code = '''python - "${BASH_SOURCE[0]}" '__MEA_ROLE__' <<'MEA_PRODUCER_LOCATION'
from pathlib import Path
import base64,hashlib,json,os,sys,types,uuid
script=Path(sys.argv[1])
if not script.is_absolute() or script.name != '.command.sh':
    raise RuntimeError('Producer location requires its declared absolute Nextflow script')
script=script.resolve(strict=True)
producer=(Path.cwd()/'capsule').resolve(strict=True)
published=script.parent/'capsule'
results=producer/'results'
published_results=script.parent/'unit-results'
# Check the shared output bound before changing any owned metadata.
if not published_results.resolve().is_relative_to(script.parent):
    raise RuntimeError('Producer location output port escapes its shared task directory')
links=[]
for root in (producer/'data',results):
    for base,dirs,files in os.walk(root,followlinks=False):
        for name in sorted(dirs+files):
            path=Path(base)/name
            if path.is_symlink():
                links.append({'source':str(path),'target':str(path.resolve(strict=True))})
for name,source in json.loads(base64.b64decode('__MEA_SERIALIZATION_MODULES__')).items():
    module=types.ModuleType(name)
    module.__file__='<mea-producer-transport>/'+name+'.py'
    sys.modules[name]=module
    exec(compile(source,module.__file__,'exec'),module.__dict__)
context=json.loads(base64.b64decode(os.environ.get('MEA_PRODUCER_CONTEXT','e30=')))
sys.modules['producer_serializations'].normalize(producer,published,published_results,
    sys.argv[2],links,context)
value={'schema_version':1,'document_type':'mea_producer_location',
       'role':sys.argv[2],'recorder_sha256':os.environ['MEA_PRODUCER_LOCATION_SHA'],'producer_root':str(producer),'published_root':str(published),'published_results':str(published_results),
       'links':links}
identity=hashlib.sha256((str(published)+'\\0'+sys.argv[2]).encode()).hexdigest()[:24]
# Publish coordination metadata atomically in its shared output port. Keeping
# it out of scratch prevents stage-out from recopying it in place.
if not published_results.resolve().is_relative_to(script.parent):
    raise RuntimeError('Producer location output port escapes its shared task directory')
published_results.mkdir(parents=True,exist_ok=True)
path=published_results/('.mea-producer-location-'+identity+'.json')
temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
try:
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o666)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,sort_keys=True,separators=(',',':'))
        stream.write('\\n');stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)
finally:
    if temporary.exists():temporary.unlink()
MEA_PRODUCER_LOCATION
'''.replace('__MEA_ROLE__', role).replace('__MEA_SERIALIZATION_MODULES__', modules)
    def identity = java.security.MessageDigest.getInstance('SHA-256')
        .digest(code.getBytes('UTF-8')).encodeHex().toString()
    [producer_location_script:code, producer_location_identity:identity]
}
