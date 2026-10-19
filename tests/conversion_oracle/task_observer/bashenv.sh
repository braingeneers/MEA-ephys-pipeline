# Test-only Bash startup hook. Preserve argv, scientific modules and return codes.
# The enclosing harness explicitly mounts this directory and sets its path.
python() {
    KS4_TASK_OBSERVER_DIR="${NXF_TASK_WORKDIR:-$PWD}/task-observer" \
    PYTHONPATH="${KS4_TASK_OBSERVER_HOOKS}${PYTHONPATH:+:$PYTHONPATH}" \
        command python "$@"
}
