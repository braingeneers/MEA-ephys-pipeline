nextflow.enable.dsl = 2

include { meaReadJson } from './json_values'

// Direct-call compatibility parsing, also usable by independent stage callers.
// All values are invocation-local; imported processes receive only their own map.
def loadLaunchSettings(Map parameters, pipelineRoot) {
    def options = new LinkedHashMap(parameters)
    def versions
    def params_keys
    def n_jobs
    def job_args
    def runmode
    def job_dispatch_args
    def preprocessing_args
    def postprocessing_args
    def curation_args
    def visualization_kwargs
    def nwb_ecephys_args
    def spikesorting_args
    if (!options.ecephys_path) {
        error 'Set --ecephys_path to a directory containing an NWB recording (or set DATA_PATH / DATA_DIR).'
    }
    println "DATA_PATH: ${options.ecephys_path}"
    println "RESULTS_PATH: ${options.results_path}"

    // Load parameters from JSON file if provided
    def json_params = [:]
    if (options.params_file) {
        json_params = meaReadJson(new File(options.params_file).toPath())
        if (!(json_params instanceof Map)) {
            error '--params_file must contain a JSON object keyed by pipeline stage.'
        }
        def unknown = json_params.keySet() - ['job_dispatch', 'preprocessing', 'spikesorting', 'postprocessing', 'curation', 'visualization', 'nwb']
        if (unknown) {
            error "Unknown stage keys in --params_file: ${unknown}. See scripts/params_no_motion.json for the nested format."
        }
        println "Loaded parameters from ${options.params_file}"
    }

    // get commit hashes for capsules
    options.capsule_versions = "${pipelineRoot}/capsule_versions.env"
    versions = [:]
    new File(options.capsule_versions as String).eachLine { line ->
        def (key, value) = line.tokenize('=')
        versions[key] = value
    }

    // container tag
    options.container_tag = "si-${versions['SPIKEINTERFACE_VERSION']}"
    println "CONTAINER TAG: ${options.container_tag}"

    // Shared runtime defaults, copied independently for every invocation. This
    // is deployment configuration, separate from the nested scientific JSON.
    def images = new LinkedHashMap(new groovy.json.JsonSlurper().parseText(
        new File("${pipelineRoot}/container_images.json").text))
    def image_overrides = options.get('container_images', [:])
    if (!(image_overrides instanceof Map)) error 'container_images must be a map keyed by base, ks4, nwb, analysis or export_verifier.'
    def unknown_images = image_overrides.keySet() - images.keySet()
    if (unknown_images) error "Unknown container_images roles: ${unknown_images}"
    images.putAll(image_overrides)
    // Keep the original report CLI option as the most specific override.
    if (options.report_container_image != null) images.analysis = options.report_container_image
    images.each { role, image ->
        if (!(image instanceof CharSequence) || !image.toString().trim())
            error "container_images.${role} must be a non-empty image reference."
        images[role] = image.toString()
    }

    params_keys = options.keySet()

    // if not specified, assume local executor
    if (!params_keys.contains('executor')) {
        options.executor = "local"
    }
    // set global n_jobs for local executor
    if (options.executor == "local")
    {
        if ("n_jobs" in params_keys) {
            n_jobs = options.n_jobs
        }
        else {
            n_jobs = -1
        }
        println "N JOBS: ${n_jobs}"
        job_args=" --n-jobs ${n_jobs}"
    }
    else {
        job_args=""
    }

    // set runmode
    if ("runmode" in params_keys) {
        runmode = options.runmode
    }
    else {
        runmode = "full"
    }
    println "Using RUNMODE: ${runmode}"

    if (options.params_file) {
        println "Using parameters from JSON file: ${options.params_file}"
    } else {
        println "No parameters file provided, using command line arguments."
    }

    // Initialize args variables with params from JSON file or command line args
    job_dispatch_args = ""
    if (options.params_file && json_params.job_dispatch) {
        job_dispatch_args = "--params '${groovy.json.JsonOutput.toJson(json_params.job_dispatch)}'"
    } else if ("job_dispatch_args" in params_keys && options.job_dispatch_args instanceof String) {
        job_dispatch_args = options.job_dispatch_args
    }

    preprocessing_args = ""
    if (options.params_file && json_params.preprocessing) {
        preprocessing_args = "--params '${groovy.json.JsonOutput.toJson(json_params.preprocessing)}'"
    } else if ("preprocessing_args" in params_keys && options.preprocessing_args instanceof String) {
        preprocessing_args = options.preprocessing_args
    }

    postprocessing_args = ""
    if (options.params_file && json_params.postprocessing) {
        postprocessing_args = "--params '${groovy.json.JsonOutput.toJson(json_params.postprocessing)}'"
    } else if ("postprocessing_args" in params_keys && options.postprocessing_args instanceof String) {
        postprocessing_args = options.postprocessing_args
    }

    curation_args = ""
    if (options.params_file && json_params.curation) {
        curation_args = "--params '${groovy.json.JsonOutput.toJson(json_params.curation)}'"
    } else if ("curation_args" in params_keys && options.curation_args instanceof String) {
        curation_args = options.curation_args
    }

    visualization_kwargs = ""
    if (options.params_file && json_params.visualization) {
        visualization_kwargs = "--params '${groovy.json.JsonOutput.toJson(json_params.visualization)}'"
    } else if ("visualization_kwargs" in params_keys && options.visualization_kwargs instanceof String) {
        visualization_kwargs = options.visualization_kwargs
    }

    nwb_ecephys_args = ""
    if (options.params_file && json_params.nwb?.ecephys) {
        nwb_ecephys_args = "--params '${groovy.json.JsonOutput.toJson(json_params.nwb.ecephys)}'"
    } else if ("nwb_ecephys_args" in params_keys && options.nwb_ecephys_args instanceof String) {
        nwb_ecephys_args = options.nwb_ecephys_args
    }

    // For spikesorting, use the parameters for the selected sorter
    def sorter = null
    if (options.params_file && json_params.spikesorting) {
        sorter = json_params.spikesorting.sorter ?: null
    }

    sorter = sorter ?: options.get('sorter', 'kilosort4')

    spikesorting_args = ""
    if (options.params_file && json_params.spikesorting) {
        def sorter_params = json_params.spikesorting[sorter]
        if (sorter_params) {
            spikesorting_args = "--params '${groovy.json.JsonOutput.toJson(sorter_params)}'"
        }
    } else if ("spikesorting_args" in params_keys && options.spikesorting_args instanceof String) {
        spikesorting_args = options.spikesorting_args
    } else if ("spikesorting_args" in params_keys) {
        error 'Use --spikesorting_args="--flag" (with an equals sign), or the nested --params_file format.'
    }

    if (sorter == null) {
        println "No sorter specified, defaulting to kilosort4"
        sorter = "kilosort4"
    }

    // Force an explicit device for CPU/GPU comparisons. Keep the pinned capsule defaults.
    if (options.torch_device) {
        if (sorter != 'kilosort4' || !(options.torch_device in ['cpu', 'cuda'])) {
            error '--torch_device must be cpu or cuda and requires --sorter kilosort4.'
        }
        if (options.get('spikesorting_args', null)) {
            error 'Use --params_file with --torch_device so the selected device cannot be overridden by raw arguments.'
        }
        def settings = json_params.spikesorting?.kilosort4 ?:
            new groovy.json.JsonSlurper().parseText(new File("${pipelineRoot}/kilosort4_defaults.json").text)
        settings.sorter.torch_device = options.torch_device
        spikesorting_args = "--params '${groovy.json.JsonOutput.toJson(settings)}'"
    }

    println "Using SORTER: ${sorter} with args: ${spikesorting_args}"

    if (runmode == 'fast'){
        preprocessing_args = "--motion skip"
        postprocessing_args = "--skip-extensions spike_locations,principal_components"
        nwb_ecephys_args = "--skip-lfp"
        println "Running in fast mode. Setting parameters:"
        println "preprocessing_args: ${preprocessing_args}"
        println "postprocessing_args: ${postprocessing_args}"
        println "nwb_ecephys_args: ${nwb_ecephys_args}"
    }


    def resources = options.get('process_resources', options.get('nrp_resources', [:]))
    if (!(resources instanceof Map)) error 'process_resources must be a map keyed by process name.'
    def effective_device = options.torch_device ?: json_params.spikesorting?.kilosort4?.sorter?.torch_device ?: 'auto'

    return [sorter: sorter, runmode: runmode, container_tag: options.container_tag,
        versions: versions, executor_mode: options.executor,
        torch_device: effective_device, resources: resources,
        input_path: options.ecephys_path, results_path: options.results_path,
        publish_overwrite: options.publish_overwrite,
        git_repo_prefix: options.git_repo_prefix,
        container_images: images, report_container_image: images.analysis,
        job_args: job_args, arguments: [job_dispatch: job_dispatch_args,
            preprocessing: preprocessing_args, sorting: spikesorting_args,
            postprocessing: postprocessing_args, curation: curation_args,
            visualization: visualization_kwargs, nwb_ecephys: nwb_ecephys_args]]
}

// Used only by the still-external capsules; never fetch a mutable branch as code.
def pinnedCloneScript() {
    return '''
clone_repo() {
    local repo_url="$1"
    local commit_hash="$2"
    echo "cloning git repo: ${repo_url} (commit: ${commit_hash})..."
    git clone "${repo_url}" capsule-repo
    git -C capsule-repo -c core.fileMode=false checkout "${commit_hash}" --quiet
    mv capsule-repo/code capsule/code
    # Retain the task-local checkout for provenance; Nextflow owns task cleanup.
}
'''
}

def processOptions(String name, Map launch) {
    def pinKeys = [job_dispatch: 'JOB_DISPATCH', preprocessing: 'PREPROCESSING',
        spikesort_kilosort25: 'SPIKESORT_KS25', spikesort_kilosort4: 'SPIKESORT_KS4',
        spikesort_spykingcircus2: 'SPIKESORT_SC2', spikesort_lupin: 'SPIKESORT_LUPIN',
        postprocessing: 'POSTPROCESSING', curation: 'CURATION', visualization: 'VISUALIZATION',
        results_collector: 'RESULTS_COLLECTOR', quality_control: 'QUALITY_CONTROL',
        quality_control_collector: 'QUALITY_CONTROL_COLLECTOR', nwb_ecephys: 'NWB_ECEPHYS',
        nwb_units: 'NWB_UNITS']
    if (!(name in pinKeys) && !(name in ['report_generation', 'burst_detection'])) {
        error "Unknown MEA process: ${name}"
    }
    def image = name in ['nwb_ecephys', 'nwb_units'] ? launch.container_images.nwb :
        name == 'spikesort_kilosort4' ? launch.container_images.ks4 :
        name == 'spikesort_kilosort25' ? "ghcr.io/allenneuraldynamics/aind-ephys-spikesort-kilosort25:${launch.container_tag}".toString() :
        name == 'report_generation' ? launch.container_images.analysis : launch.container_images.base
    def result = [container: image,
        executor_mode: launch.executor_mode, capsule_pin: launch.versions[pinKeys[name]] ?: 'checkout']
    def resources = launch.get('resources', [:]).get(name, [:])
    if (!(resources instanceof Map)) error "Resources for ${name} must be a map."
    result.resources = new LinkedHashMap(resources)
    if (name == 'spikesort_kilosort4') result.torch_device = launch.torch_device ?: 'auto'
    def argumentName = name.startsWith('spikesort_') ? 'sorting' : name
    if (launch.arguments.containsKey(argumentName)) result.args = launch.arguments[argumentName]
    if (name in ['preprocessing', 'spikesort_kilosort25', 'spikesort_kilosort4',
                 'spikesort_spykingcircus2', 'spikesort_lupin', 'postprocessing', 'curation']) {
        result.job_args = launch.job_args
    }
    if (name in ['spikesort_kilosort25', 'spikesort_spykingcircus2', 'spikesort_lupin',
                 'quality_control', 'quality_control_collector']) {
        result.clone_script = pinnedCloneScript()
        result.git_repo_prefix = launch.git_repo_prefix
    }
    if (name in ['results_collector', 'quality_control_collector', 'nwb_units',
                 'report_generation', 'burst_detection']) {
        result.results_path = launch.results_path
        result.publish_overwrite = launch.publish_overwrite == null ? null :
            Boolean.parseBoolean(launch.publish_overwrite.toString())
    }
    if (name == 'results_collector') result.input_path = launch.input_path
    return result
}
