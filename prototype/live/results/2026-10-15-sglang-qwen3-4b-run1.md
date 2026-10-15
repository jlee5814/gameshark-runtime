# Live inference acceptance, 2026-10-15T06:46:21-0400

Commit b407a1d; local target `mlx-community/Qwen3-4B-4bit`.

| Group | Scenario | Result | Failed checks |
|---|---|---|---|
| local | generate_latency_and_replay_identity | pass |  |
| local | concurrent_duty_and_cancel | pass |  |
| local | replay_without_dispatch | pass |  |
| local | concurrency_overload_recovery | pass |  |
| local | restart_owner_recovery | pass |  |
| local | stale_owner_fenced | pass |  |
| policy_fake_cloud | local_only_never_egresses | pass |  |
| policy_fake_cloud | cloud_ok_without_grant | pass |  |
| policy_fake_cloud | paid_estimate_without_soft_grant | pass |  |
| policy_fake_cloud | paid_estimate_with_soft_grant | pass |  |
| cloud | all | not run | GAMESHARK_CLOUD_OPT_IN=1 not set |

Time to first token p50 0.065 s, max 0.0672 s; completion p50 0.1366 s.

Server answers off topic (prefix cache anomaly, not a runtime check): local/restart_owner_recovery.

Secret scan: no secret configured.
