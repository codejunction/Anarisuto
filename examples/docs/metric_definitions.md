# Metric definitions

These are the agreed definitions. When a question uses one of these words, use this meaning
unless the person asking says otherwise.

## Gross revenue
Sum of `orders.amount` for orders with status `completed` or `refunded`.
Cancelled orders are never revenue.

## Net revenue
Gross revenue minus `orders.refund_amount`.
"Revenue" on its own means **net revenue**.

```sql
select sum(amount) - sum(refund_amount)
from orders
where status in ('completed', 'refunded')
```

## Refund rate
Refunded orders divided by orders that were not cancelled, as a percentage.

## Average order value (AOV)
Net revenue divided by the number of orders that were not cancelled.

## Attainment
Net revenue for a region in a fiscal quarter divided by that region's target for the
quarter, as a percentage. Targets are in the `targets` table.

## Active customer
A customer with at least one completed order in the period.
