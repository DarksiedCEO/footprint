#!/usr/bin/env bash
# Remediation item 15: the explicit regression tests, one group per item.
# One -k per run: pytest keeps only the last -k given.
#   bash qa/regression.sh            (output: qa/regression.out)
set -uo pipefail
cd "$(dirname "$0")/.."
export FOOTPRINT_REQUIRE_IBAPI=1
fail=0
echo "source: $(git rev-parse HEAD) tree-dirty: $(git status --porcelain -- src tests | wc -l) files"
# One run per group: the printed counts and the verdict come from the same run. Tests
# run as `python -m pytest` so a copied tree tests its own source (independent audit
# A-17). A group that selects no tests exits 5 and is FAILED.
run() {
  local name=$1; shift
  local out code
  echo "=== $name"
  out=$(uv run python -m pytest -q -p no:cacheprovider "$@" 2>&1)
  code=$?
  printf '%s\n' "$out" | grep -E "passed|failed|error|deselected" | tail -1
  if [ "$code" -ne 0 ]; then echo "    FAILED (exit $code)"; fail=1; fi
}
B=tests/brokers/test_paper_boundary.py
D=tests/execution/test_disconnect_policy.py
C=tests/execution/test_calendar_fail_closed.py
A=tests/live/test_alarm_failure_and_modes.py
CH=tests/live/test_channels.py
run "live-account rejection" $B -k "non_paper or live_login or live_account or refuses_a_live_account_reported"
run "port 4001 rejection" $B -k "live_port or live_ports or live_account_or_port"
run "configuration-bypass attempts" $B -k "env_file or yaml_key or setting_anywhere or command_lines or forced or smuggled" tests/live/test_live_app.py::test_settings_smuggled_past_the_check_are_refused_by_build
run "IBKR disconnect" $D -k "loss or doubt" tests/brokers/test_ibkr_adapter.py::test_the_runner_is_told_at_the_loss_and_before_ready_again
run "failed reconciliation" $D -k "never_agrees or stop_filled"
run "expired calendar" $C -k "expired or past_the_calendar"
run "holiday blocking" $C -k "holiday or half_days or no_trade"
run "Twilio timeout" $A -k "hangs or hanging"
run "Twilio rejection" $A -k "rejected or repeated or recovers or never_rung"
run "Telegram fallback (Telegram down)" $A $CH -k "telegram_down or telegram_fails or hung_telegram"
run "SMS fallback" $CH $A -k "sms_fallback or telegram_fails or sms_down or each_channel_down"
run "Telegram-only degraded mode" $A -k "degraded or telegram_only or unattended or attended or quiet or full_alerting"
run "no network in tests" $CH -k "cannot_reach or proxy"
run "H-2 entry protected during an outage (attached stop)" tests/execution/test_attached_stop.py tests/brokers/test_ibkr_adapter.py tests/brokers/test_ibkr_binding.py -k "attached or bracket or outage or held or real_fill"
run "D37 outage excluded from the failsafe, flapping cannot hold it off" $D -k "flapping or only_the_outage or mid_step"
run "K-1/K-2/K-3 a stop being cancelled: never protection, never doubled, no false latch" tests/execution/test_held_back_stop.py tests/execution/test_attached_stop.py tests/brokers/test_ibkr_adapter.py -k "cancelling or pending_cancel or kept_never_doubled or never_doubles or uncertain_stop or copies or someone_else or stale_pending_submit or pending_its_inactive"
run "L2 audit: a doubt as our cancel lands, refused cancels, stale clocks, exit reasons" tests/execution/test_held_back_stop.py tests/execution/test_attached_stop.py -k "doubt_as_our_exit or positions_settle or refused or stale_clock or no_stuck_clock or keeps_the_reason or by_reconcile or alarms_once or counts_from_then or will_not_cancel_is_kept"
run "M3 audit: refused cancels end the exit, honest exit reasons" tests/execution/test_held_back_stop.py tests/brokers/test_ibkr_adapter.py -k "stale_quote_then or stale_gap or sent_nothing or refused_after_pending or sent_its_cancel or mid_step"
run "N4 audit: refusals never restart escalation, stale messages, dropped exits, bookings" tests/execution/test_held_back_stop.py tests/brokers/test_ibkr_adapter.py -k "repeated_cancel_refusals or wanting_out or stale_submitted_between or new_pending_cancel or resent_cancel or failsafe_exit or failsafe_flatten_after_our or dropped_exit or unanswered_reconcile or ends_is_no_refusal or open_order_is_counted or summed_over_copies"
run "O5 audit: CANCEL STUCK when reconcile agrees but the stop will not cancel; stale-quote alarm once" tests/execution/test_held_back_stop.py -k "tells_andre or once_per_episode or landing_between_steps or stale_look_then or old_refusal"
run "P6 final pass: CANCEL STUCK timing, silent feed critical, degraded banner" tests/execution/test_held_back_stop.py tests/live/test_alarm_failure_and_modes.py -k "second_hand_over or silent_quote or own_hand_overs or says_so_at_start or own_cancel_stuck"
exit $fail
