#!/usr/bin/env bash
# Remediation item 15: the explicit regression tests, one group per item.
# One -k per run: pytest keeps only the last -k given.
#   bash qa/regression.sh            (output: qa/regression.out)
set -uo pipefail
cd "$(dirname "$0")/.."
export FOOTPRINT_REQUIRE_IBAPI=1
fail=0
run() {
  local name=$1; shift
  echo "=== $name"
  if uv run pytest -q -p no:cacheprovider "$@" 2>&1 | grep -E "passed|failed|error" | tail -1; then :; fi
  uv run pytest -q -p no:cacheprovider "$@" >/dev/null 2>&1 || { echo "    FAILED"; fail=1; }
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
exit $fail
