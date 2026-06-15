<!-- Input: reconnect, stale-data, execution recovery, and risk regression coverage -->
<!-- Output: release-readiness evidence map for high-risk runtime paths -->
<!-- Pos: reconnect and risk regression evidence guide -->
<!-- Update this header and docs/README.md when this document changes. -->

# Regression Evidence

This document maps the highest-risk runtime paths to concrete regression coverage for release readiness. It is evidence for existing automated tests, not proof of live exchange behavior.

## Validation Commands

Focused validation used for this evidence pass:

```text
uv run pytest tests/test_ws_market.py tests/test_ws_user_data.py -q
64 passed

uv run pytest tests/test_execution.py -q
95 passed

uv run pytest tests/test_main_shutdown.py tests/test_protective_stop.py tests/test_risk_manager.py tests/test_exchange.py tests/test_signal.py -q
178 passed
```

Full validation:

```text
uv run pyright src/
0 errors, 0 warnings, 0 informations

uv run pytest -q
472 passed
```

Re-run these commands on the exact commit that will receive the release tag.

## Coverage Map

| Risk area | Evidence | What it protects |
| --- | --- | --- |
| Market WebSocket stale data | `tests/test_ws_market.py::TestStaleDataDetection`; `tests/test_ws_market.py::TestHandleMessage::test_handle_message_depth_does_not_refresh_stale_timer`; `tests/test_signal.py::test_pressure_mode_detects_stale_book_ticker_even_if_depth_is_fresh`; `tests/test_main_shutdown.py::test_evaluate_symbol_side_resets_pressure_dwell_on_market_stale` | Prevents stale or depth-only updates from being treated as fresh execution input. |
| Market/User Data reconnect callback | `tests/test_ws_market.py::TestReconnectCallback::test_on_reconnect_called_after_reconnect`; `tests/test_ws_user_data.py::TestReconnectCallback::test_on_reconnect_called_after_reconnect`; `tests/test_ws_market.py::TestReconnection::test_reconnect_count_initial`; `tests/test_ws_user_data.py::TestReconnection::test_reconnect_count_initial` | Ensures reconnect completion can trigger upper-layer calibration or refresh behavior. |
| User data parsing and unknown events | `tests/test_ws_user_data.py::TestParseOrderUpdate`; `tests/test_ws_user_data.py::TestParseAccountUpdate`; `tests/test_ws_user_data.py::TestParseOrderStatus::test_parse_unknown_status`; `tests/test_ws_user_data.py::TestHandleMessage::test_handle_unknown_event_ignored` | Keeps user-stream parsing tolerant of irrelevant events while preserving order/update semantics used by execution state. |
| Execution timeout and cancel/fill races | `tests/test_execution.py::TestCheckTimeout::test_timeout_order_missing_after_concurrent_fill_does_not_revert_to_waiting`; `tests/test_execution.py::TestCheckTimeout::test_timeout_cancel_failure_keeps_waiting_context_and_retries_after_backoff`; `tests/test_execution.py::TestCheckTimeout::test_timeout_with_zero_strategy_cooldown_keeps_context_until_grace`; `tests/test_execution.py::TestCheckTimeout::test_timeout_regular_order_keeps_context_until_grace` | Prevents timeout/cancel/fill races from losing context or returning to an unsafe `WAITING` state. |
| Orphaned execution state recovery | `tests/test_execution.py::TestCheckTimeout::test_on_signal_recovers_orphaned_canceling_state`; `tests/test_execution.py::TestCheckTimeout::test_on_signal_orphan_recovery_waits_for_recent_fill_reconcile` | Recovers `WAITING/CANCELING` states with missing order IDs without immediately reusing stale position data after recent fills. |
| Panic close path | `tests/test_execution.py::TestPanicClose::test_compute_panic_qty_basic`; `tests/test_execution.py::TestPanicClose::test_compute_panic_qty_uses_min_qty_and_caps_by_position`; `tests/test_execution.py::TestPanicClose::test_on_panic_close_creates_risk_intent`; `tests/test_execution.py::TestPanicClose::test_on_panic_close_recovers_orphaned_canceling_state_immediately`; `tests/test_execution.py::TestPanicClose::test_on_panic_close_waits_for_recent_fill_reconcile`; `tests/test_execution.py::TestPanicClose::test_risk_timeout_uses_ttl_override_without_decay` | Verifies panic-close sizing, reduce-only risk intent creation, and recovery behavior around orphaned or recently filled states. |
| Reduce-only blocking and repeated submission | `tests/test_execution.py::TestReduceOnlyBlock::test_on_signal_skips_while_reduce_only_block_active_before_recheck`; `tests/test_execution.py::TestReduceOnlyBlock::test_on_signal_rechecks_reduce_only_block_and_resumes_after_release`; `tests/test_execution.py::TestOnOrderPlaced::test_order_placed_4118_latches_reduce_only_block`; `tests/test_exchange.py::TestReduceOnlyBlockInspection::test_inspect_reduce_only_block_detects_covering_orders`; `tests/test_exchange.py::TestReduceOnlyBlockInspection::test_inspect_reduce_only_block_ignores_wrong_side_and_undercovered_qty` | Avoids repeated invalid reduce-only submissions when same-side close orders already cover the tradable position. |
| Risk distance and global rate limits | `tests/test_risk_manager.py::TestRiskDistance`; `tests/test_risk_manager.py::TestGlobalRateLimit` | Covers missing-price handling, liquidation-distance trigger decisions, and order/cancel rate-limit behavior. |
| Pressure-mode risk semantics | `tests/test_main_shutdown.py::test_evaluate_side_risk_does_not_promote_pressure_passive_signal`; `tests/test_main_shutdown.py::test_evaluate_side_risk_does_not_trigger_preempt_for_pressure_passive` | Confirms ordinary risk triggers do not rewrite pressure-mode passive semantics; forced execution remains the `panic_close` responsibility. |
| Protective-stop external takeover | `tests/test_protective_stop.py::TestProtectiveStopSync::test_sync_skips_when_external_close_position_algo_exists`; `tests/test_protective_stop.py::TestProtectiveStopSync::test_sync_skips_when_external_reduce_only_stop_exists`; `tests/test_protective_stop.py::TestProtectiveStopSync::test_sync_logs_when_multiple_external_stops_exist`; `tests/test_protective_stop.py::TestProtectiveStopSync::test_sync_startup_logs_existing_external_stop`; `tests/test_protective_stop.py::TestProtectiveStopSync::test_sync_cancels_own_order_when_external_close_position_exists`; `tests/test_protective_stop.py::TestInvalidExternalStop::test_cancels_invalid_external_short_stop`; `tests/test_protective_stop.py::TestInvalidExternalStop::test_valid_external_keeps_takeover`; `tests/test_protective_stop.py::TestInvalidExternalStop::test_invalid_external_ignores_latch` | Avoids conflicting with valid external stops, cancels invalid external stops when appropriate, and preserves takeover behavior. |
| Protective-stop scheduling | `tests/test_main_shutdown.py::test_protective_stop_debounce_classification`; `tests/test_main_shutdown.py::test_schedule_debounce_task_can_be_cancelled`; `tests/test_main_shutdown.py::test_schedule_executing_task_not_cancelled`; `tests/test_main_shutdown.py::test_schedule_no_concurrent_sync`; `tests/test_main_shutdown.py::test_schedule_triple_trigger_no_concurrent` | Ensures debounce-stage tasks can merge safely while REST execution is not canceled or run concurrently for the same symbol. |

## Remaining Live-System Limits

These automated tests do not prove:

- Binance mainnet liquidity or fill quality.
- Real network outage timing and recovery latency.
- Exchange-side stop-order behavior across every symbol and margin mode.
- Telegram delivery behavior under every provider/network condition.
- Operator ability to manually intervene during a live incident.

For those areas, use [Small-Size Production Validation](production-validation.md) and record only sanitized evidence.
