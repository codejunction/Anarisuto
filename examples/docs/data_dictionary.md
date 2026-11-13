# Sales database: data dictionary

Source: the `demo_sales` MCP server. All dates are ISO strings (`YYYY-MM-DD`) in UTC.

## orders
One row per order line.

| column | meaning |
|---|---|
| id | order id |
| order_date | date the order was placed |
| customer_id | joins to `customers.id` |
| product_id | joins to `products.id` |
| qty | units ordered |
| amount | gross amount = qty × unit price at order time, before refunds |
| status | `completed`, `refunded` or `cancelled` |
| refund_amount | money returned to the customer; 0 unless status is `refunded` |

## customers
| column | meaning |
|---|---|
| id | customer id |
| region | `north`, `south`, `east` or `west`. The region of a sale is the customer's region. |
| segment | `smb` or `enterprise` |
| signup_date | first day the customer could order |

## products
| column | meaning |
|---|---|
| category | `software`, `hardware` or `services` |
| unit_price | current list price |

## targets
Quarterly net revenue target per region. `fiscal_quarter` looks like `FY2026-Q3`
(see the fiscal calendar). There is no target for a region before it opened.

## Joins
- region of an order: `orders.customer_id = customers.id`, then `customers.region`
- category of an order: `orders.product_id = products.id`, then `products.category`
