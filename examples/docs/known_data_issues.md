# Known data issues

Read before reporting on refunds or comparing regions over time.

1. **East refunds before 1 July 2025 are missing.** The east region's refund feed was not
   connected until 1 July 2025. Refunded orders before that date show as `completed` with
   `refund_amount = 0`. Net revenue and refund rate for east in FY2026-Q1 are overstated and
   should not be compared with other regions or later quarters without saying so.

2. **West opened on 1 October 2025** (FY2026-Q3). It has no orders and no targets before then.
   Growth figures for west that start before FY2026-Q3 are meaningless.

3. **The data ends on 30 September 2026.** FY2027-Q2 is the last complete quarter.

4. `products.unit_price` is the current list price. Use `orders.amount` for money, never
   `qty × unit_price`.
