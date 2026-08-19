# Post-freeze scripted mechanism assessment

This is a deterministic, Provider-free mechanism evaluation. It is not a formal benchmark, does not replay
any frozen `run-001`, and is not merged into the original 11/13 development denominator.

## Scope

- `evaluation_role`: `post_freeze_scripted_mechanism_evaluation`
- `formal_benchmark`: `false`
- `metric_eligible`: `false`
- `provider_calls`: `0`
- `frozen_run_001_replayed`: `false`
- Original development validated Candidate rate: `11/13`

## Scripted scenarios

- `ff008_root_cause_scripted_reproduction`: submitted_after_post_validation_edit_was_locked
- `reopen_invalidates_validation`: revalidated_and_submitted
- `validation_failure_retention`: failure_retained_until_script_limit
- `deterministic_context_projection`: submitted_with_protocol_valid_projected_context

The FF-008 scenario reproduces only the post-validation closure-failure mechanism. It does not rerun or
reclassify the frozen FF-008 attempt and does not claim that FF-008 was solved again.

## Computed metrics

| Metric | Value |
|---|---:|
| Scripted scenarios | 4 |
| Ready-state illegal action attempts | 2 |
| Ready-state illegal action blocks | 2 |
| Ready-state illegal action block rate | 1.000000 |
| Stale validation exposure count | 0 |
| State-card policy checks | 6 |
| State-card policy consistency rate | 1.000000 |
| Required-context retention checks | 13 |
| Required-context retention rate | 1.000000 |
| Model calls | 14 |
| Cumulative raw model-visible characters | 1219944 |
| Cumulative projected model-visible characters | 720905 |
| Cumulative character reduction ratio | 0.409067 |
| Maximum raw characters per call | 150615 |
| Maximum projected characters per call | 72796 |
| Average raw characters per call | 87138.857143 |
| Average projected characters per call | 51493.214286 |
| Configured projection limit per call | 80000 |
| Calls exceeding projection limit | 0 |

Character counts are deterministic JSON-serialized message sizes. Cumulative totals sum model-visible context
across all calls; they are not a single prompt, token counts, or evidence of Provider cost reduction. Required
evidence remains visible even when a projected call exceeds the configured limit, and such calls are counted.
