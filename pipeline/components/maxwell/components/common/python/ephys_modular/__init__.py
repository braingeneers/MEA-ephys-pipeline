"""Portable contracts and verified object I/O for the additive Ephys workflows."""

__version__ = "0.1.8"

# Owning task commands must retain this exit code. Shared Nextflow policies must
# terminate it instead of multiplying an exhausted object-operation retry budget.
TRANSFER_FAILURE_EXIT = 76
