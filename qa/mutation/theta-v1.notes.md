# theta-v1 mutation notes

## First run (source 5973889, clean tree)

67 mutants: 63 killed, 4 survived.

- **#22 contract id not recorded, #23 contracts not recorded:** real gaps in the theta
  group. The pipeline tests checked both fields, but the theta group's own tests did not.
  Fixed: test_a_normal_entry_is_allowed_and_carries_its_numbers now asserts the contract
  id and contracts, and test_decay_scales_with_the_contracts asserts 1 and 3.
- **#43 week counted off by one day** (`expiry.day // 7 + 1`): a real gap. The two
  formulas differ only on days 7, 14, 21 and 28, and no test date fell there. Fixed with
  two quarterly-month Fridays: 2025-03-21 (third Friday, American) and 2025-03-14
  (second Friday, not American).
- **#38 bisection off-centre** (`fmean((lo, hi, hi))`): **argued equivalent.**
  - The answer depends only on the invariants, not on where in the bracket the trial
    point falls. Those invariants are: value(lo) < quoted <= value(hi) after the range
    checks, and every step keeps one end on each side.
  - Any interior trial point shrinks the bracket, so the loop ends when the bracket's
    ends are adjacent floats. That happens when the trial point equals one of them.
  - Black-76 price is strictly increasing in volatility while time is left. So the
    final pair of adjacent floats straddling the solution is the same pair whichever
    interior split is used.
  - The returned value is then the trial point that equals an end. With an off-centre
    split that can be the other end of the same adjacent pair: one float, about 1e-16
    relative, which is far below any test tolerance and has no meaning for a decay
    estimate.
  - Where the price is flat in volatility (no time value), many volatilities share the
    price, so any of them is an equally valid answer (documented in the module).
