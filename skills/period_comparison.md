---
description: Comparing a metric between two periods (quarter over quarter, year over year, growth)
---
1. Turn both periods into exact start and end dates. Use the fiscal calendar for fiscal
   periods and the `fiscal_period` tool when you need the quarter a date falls in.
2. Check that both periods are complete and that every group being compared existed in both
   (a region that opened mid-way has no baseline).
3. Run one query that returns the metric for both periods side by side, grouped as asked.
4. Report the value for each period, the absolute change and the percentage change.
   Percentage change is (new − old) / old. If old is zero or missing, say "no baseline"
   instead of a percentage.
5. Name the largest contributor to the change.
