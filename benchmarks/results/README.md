# Baseline results

Each run directory records the first valid model attempt for one task. Results are immutable whether the attempt
succeeds or fails. Evidence files vary across historical Runner versions: validation evidence may be recorded in the
trajectory, tool calls, validation summary, or separate pytest and Ruff logs when those logs were produced.
Eligibility is based on receiving at least one assistant response; API call counters alone do not make a run eligible.
Generated Python, pytest, and Ruff caches are excluded from changed-file classification.

Credentials and API keys must never be stored in this directory.
