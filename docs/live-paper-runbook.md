# Running Footprint on the IBKR paper account

Everything here runs on your Mac, beside IB Gateway. Paper only, and no setting can
change that: the paper rule is in the code (`brokers/ibkr/paper.py`), not in config.
The program (and the read-only check) refuses, before anything is created, an account
id that does not begin with `DU`, IB Gateway's live port 4001 or TWS's live port 7496,
and a login that manages any non-paper account. There is no live switch to find.

## Before the first run

1. IB Gateway is running, logged in to the **paper** account, API settings as in the
   paper checklist (port 4002, localhost only, Read-Only off, "Maintain and resubmit
   orders when connection is restored" **unchecked**).
2. `.env` in the repo folder has `BROKER_HOST`, `BROKER_PORT`, `BROKER_ACCOUNT_ID`,
   `BROKER_CLIENT_ID` (the same values the read-only check used). Never commit it.
3. The read-only check passes: `uv run python -m footprint.brokers.ibkr.check`.
4. Real-time CME data on the paper login. Without it every quote is refused: the chain
   stays empty, so no new trade is ever opened. But nothing can be priced either: if a
   position is left over from an earlier run, the system will not sell it blind and
   asks you (an alarm) to flatten it by hand in IBKR.
5. A news calendar CSV (`name,start,end`, ISO times **with offsets**, e.g.
   `CPI,2026-10-14T08:28:00-04:00,2026-10-14T08:32:00-04:00`), or decide to run
   without one (`--no-blackouts`).
6. Phone alarms (D20): all six keys for FULL alerting. Without them the program refuses
   to start unless you choose a degraded mode explicitly (see "Degraded alerting").

## Phone alarms (D20)

Every alarm goes to Telegram; if Telegram fails it goes by SMS instead; the critical
ones also ring your phone: a halt, the kill switch, a position without its stop or
needing hands, IBKR refusing, failing for good or **the connection lost**, a stuck
resync, market data failing, an expired exchange calendar, an alarm channel down, and
unattended degraded operation. Each kind rings at most once every
`call_cooldown_seconds` (30 minutes), and one kind ringing never silences another.
Expect calls you may not want at first: IB Gateway's nightly restart is a lost
connection, so it rings once a night; a stuck resync re-alarms every 5 minutes, so it
rings every 30 minutes while it lasts, even while flat. Every alarm is also in the
terminal and `state/alarms.log`.

If a channel fails (Twilio rejects a call, Telegram is down, a request hangs past
`channel_timeout_seconds`), "ALARM CHANNEL DOWN: <channel>" goes at once to the record
and to every **other** channel (a dead call line is reported by Telegram and SMS; a
dead Telegram by SMS and a call), and "alarm channel back" when it works again. A
failed call is not retried. A hung call holds the call line for at most
`channel_timeout_seconds` (8 s; the request itself, a slow name lookup included, is
abandoned on its own thread at that deadline, never waited for) and never holds back Telegram or SMS; while it is still
out, the next call fails at once (one stuck request per channel at most) and is
reported, and calls are tried again once it ends. `status` shows each channel's state.

All six keys go in `.env` only (never in chat, never committed):

1. Telegram: in Telegram, message **@BotFather**, `/newbot`, and put the token it
   gives you in `TELEGRAM_BOT_TOKEN`. Send your new bot any message, then run
   `uv run python -m footprint.live.alarm_setup chat-id` and copy the
   `TELEGRAM_CHAT_ID=...` line it prints into `.env`.
2. Twilio (SMS and calls): from the Twilio console, `TWILIO_ACCOUNT_SID` and
   `TWILIO_AUTH_TOKEN`; a Twilio number that can send SMS and call in
   `TWILIO_FROM_NUMBER`; your mobile in `ALARM_PHONE_NUMBER`. Both numbers in
   +1XXXXXXXXXX form. A trial Twilio account only reaches verified numbers and prefixes
   messages and calls with a trial notice. **US SMS needs the sending number
   registered** (A2P 10DLC for a local number, or toll-free verification): without it
   Twilio accepts the text and the carrier silently blocks it.
3. Check: `uv run python -m footprint.live.alarm_setup test` sends one test alarm per
   channel (Telegram, SMS, a call). Telegram prints OK or FAILED; SMS and the call print
   ACCEPTED or FAILED, because Twilio accepting them is not delivery: **check that the
   text arrived and the phone rang.** A malformed key (a space, a comment after the
   value) is refused by name before anything is sent.
4. Fallback check: `uv run python -m footprint.live.alarm_setup test-fallback` makes
   Telegram fail on the Mac (nothing is sent to Telegram) and runs the real alarm
   path: you should get a text with the test alarm, a text "ALARM CHANNEL DOWN:
   Telegram", and one call.

These commands are the only real deliveries: the automated tests cannot reach
Telegram, Twilio or anything else beyond the machine (a test guard blocks it).

## Degraded alerting

Two explicit flags, never the default, and never shown as full coverage:

- `--alarms-telegram-only`: Telegram only (the two Telegram keys), no SMS fallback,
  no calls. Every Telegram message starts with `[DEGRADED_ALERTING]`. Test it with
  `alarm_setup test --telegram-only`.
- `--alarms-local-only`: nothing reaches your phone (terminal and `alarms.log`).

Either one: the program refuses to start without an interactive terminal; it prints
a DEGRADED_ALERTING banner; `status` shows it first; and **unattended trading is
blocked**: after `degraded_attendance_minutes` (15) with nothing typed at the prompt,
it stops new entries (an UNATTENDED alarm; anything open is still managed) until you
type `clear <name>` again. It warns a few minutes before. Any command, or `here`,
counts as attended.

Nothing prints a token, a password or a phone number, and an error never shows a URL
(Telegram's carries the token).

## Start

First run only (creates `state/ledger.json`):

    uv run python -m footprint.live --blackouts news.csv --new-ledger

Every later run:

    uv run python -m footprint.live --blackouts news.csv

It starts reconciling and **sends nothing** until you clear it. At the prompt:

| Type | Does |
|---|---|
| `status` | Alerting mode and each channel, cleared or not, connection, front future, bars loaded, chain size, reconcile/halt state, position |
| `clear Andre` | Allows entries (logged as an alarm with your name) |
| `here` | Says you are at the terminal (keeps a degraded run attended) |
| `quit` | Stops cleanly (also Ctrl-C) |

Loading 35 days of history takes about a minute after the connection opens
(`status` shows the bar count growing from 0).

## Stopping with a position open

`quit` (or Ctrl-C) stops managing at once; it does not flatten. It warns you if
anything is held or working. After that only the resting stop at IBKR protects the
position, and it is a DAY order: it lasts until IBKR's trading session ends. The next
start flattens anything still held (priced at the market once a quote arrives; never
sold blind). Flatten by hand in IBKR, or check the stop there, before you walk away.

## Gateway restarts and disconnects

IB Gateway restarts itself every night. On a lost connection (D35, as decided):

- New entries stop at once, and nothing is sent: no order, cancel, modify or flatten.
- Orders already resting at IBKR (the protective stop) are left as they are.
- Trading resumes only after IBKR is back, resynced, **and** a reconcile agrees with
  what IBKR holds. If the stop filled during the outage, the reconcile finds it and
  books the close. A reconcile that never agrees never lets trading resume.
- The outage itself does not count against the 30-second "unmanaged" failsafe
  (D37, confirmed): its clock moves on by exactly the outage's length, so time
  unmanaged before or after still counts, and a protected position is not flattened
  on reconnect just because of the outage.
- An entry still working at IBKR when the connection drops carries its stop attached
  (D38): IBKR should activate the stop the moment the entry fills, connected or not
  (IBKR's side is UNVERIFIED until checklist 27 in paper). The entry never works
  without its stop: if the stop is refused or dies, the entry is cancelled. After the
  reconnect the reconcile finds any fill and flattens it.
- It is not a halt. "IBKR connection lost" rings the phone (critical).

After 5 failed reconnects in a row it alarms once that a person is needed and retries
less often (D32). A halt (a real disagreement: something held that should not be, an
order that will not cancel) also latches the kill switch. The halt itself clears with a
restart, but the kill switch does NOT: it is written to `state/ledger.json` and survives
restarts, so the system makes no new entries until it is cleared. There is no operator
command for that yet: clearing it is `Ledger.clear_kill_switch("<your name>")` from
Python, after you have looked at why, and it also resets the drawdown peak to current
equity (so the drawdown breaker starts again from today's equity). Never edit
`ledger.json` by hand. (An operator command, and whether an operational latch should
reset the peak, are open decisions for Andre: see docs/decisions-pending.md, P6-1.)

## Where things go (all in `state/`, git-ignored)

- `ledger.json`: equity, drawdown peak, breakers, kill switch. Never delete it to
  "reset"; a missing ledger is refused unless `--new-ledger`.
- `journal/`: every decision and trade.
- `alarms.log`: every alarm, one JSON line each (also printed in the terminal), whether
  or not it reached your phone.

## Holidays and early closes

`instruments/nq.yaml` lists the exchange's closed days and early closes through
`calendar_through`. On an early-close day the system stops new entries and flattens
5 minutes before that day's close (e.g. 09:55 PT for a 10:00 close).

US-holiday sessions (MLK, Presidents, Memorial, Juneteenth, July 3, Labor Day,
Thanksgiving) are `no_trade_days`: the exchange has a short session, but the stock
market is closed, so Footprint treats them as closed (no entry, and they never count
as the prior day) until they are reviewed and approved. The day after Thanksgiving and
Christmas Eve still trade until their early close.

**Fail closed:** past `calendar_through` every day is treated as closed: no trades at
all. A month before, it alarms once a day; once expired, "EXCHANGE CALENDAR EXPIRED"
rings the phone daily. To resume, extend the lists and `calendar_through` from CME's
calendar (checklist 23), add the next year's Easter Sunday to `easter_sundays`, and
restart. The file is checked at start: every standard closure in `holiday_rules` (New
Year, MLK, Presidents, Good Friday, Memorial, Juneteenth, July 4, Labor, Thanksgiving,
Christmas) up to `calendar_through` must be listed as a closure, and the day after
Thanksgiving and Christmas Eve as early closes, or the program refuses to start;
moving `calendar_through` alone is refused.

## Alarms you should expect at first

- `exchange calendar ends ...`: a month before `calendar_through`; extend nq.yaml.
- `IBKR connection lost` (a call): IB Gateway's nightly restart.
- `MARKET DATA: ... failed (IBKR 354 ...)`: no real-time subscription.
- `no option on the ... future expires in 1-3 days`: in a roll week (D27).
- `IBKR SESSION KEEPS FAILING`: Gateway closed, logged out, or the Mac asleep (D32).
