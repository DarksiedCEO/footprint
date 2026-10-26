# backtest-history-v1 review round 2: mutants argued equivalent

## r2 #9: the closed-day search runs 400 days past the calendar (`backtest/rolls.py`, `blocked_days`)

`if day > instrument.calendar_through:` changed to `if day > instrument.calendar_through + timedelta(days=400):`.

The loop advances from a roll session while `is_tradable_day(day, instrument)` is false
and refuses once the day passes the bound. `is_tradable_day` is false for every day after
`calendar_through` (core/calendar.py: `calendar_from <= day <= calendar_through` is part
of it). So for any roll session the loop either stops at the same tradable day on or
before `calendar_through` (both versions), or finds no tradable day and raises the same
ValueError (the original as soon as it passes `calendar_through`, the mutant 400 days
later). The outcome is identical; only the number of loop turns differs. Not proven by a
test; argued.
