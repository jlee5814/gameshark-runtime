# Live inference acceptance, 2026-10-15T06:47:10-0400

Commit b407a1d; local target `mlx-community/Qwen3-4B-4bit`.

| Group | Scenario | Result | Failed checks |
|---|---|---|---|
| local | generate_latency_and_replay_identity | pass |  |
| local | concurrent_duty_and_cancel | FAIL | polls_continue_while_streaming, notification_and_control_while_streaming, drain_exit_5, provider_cancelled, cancel_within_grace_2s, settled_lost_event_pending_no_message, successor_same_call_id_attempt_2, successor_effect_gate |
| local | replay_without_dispatch | pass |  |
| local | concurrency_overload_recovery | pass |  |
| local | restart_owner_recovery | pass |  |
| local | stale_owner_fenced | pass |  |
| policy_fake_cloud | local_only_never_egresses | pass |  |
| policy_fake_cloud | cloud_ok_without_grant | pass |  |
| policy_fake_cloud | paid_estimate_without_soft_grant | pass |  |
| policy_fake_cloud | paid_estimate_with_soft_grant | pass |  |
| cloud | all | not run | GAMESHARK_CLOUD_OPT_IN=1 not set |

Time to first token p50 0.0621 s, max 0.0675 s; completion p50 0.1377 s.

Server answers off topic (prefix cache anomaly, not a runtime check): local/generate_latency_and_replay_identity, local/concurrent_duty_and_cancel, local/restart_owner_recovery.

Secret scan: no secret configured.
