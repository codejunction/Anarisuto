---
description: Checking whether the data can be trusted before reporting a number
---
1. Search the knowledge base for known data issues that touch the tables, regions or dates
   in the question.
2. Check the date range of the data with `min(order_date)` and `max(order_date)` and make
   sure the period asked about is inside it.
3. Count rows per group in the period. A group with very few rows, or none, needs a caveat.
4. If a known issue affects the answer, either exclude the affected rows and say so, or keep
   them and say how they bias the result. Never leave it unmentioned.
