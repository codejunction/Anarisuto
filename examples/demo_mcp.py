"""Demo data source: a small, made-up sales database served over MCP (stdio).

Tables: customers, products, orders, targets. Generated from a fixed seed, so every run sees
the same data. The connection is read-only at the database level (PRAGMA query_only).
Built-in quirks for the agent to find: refunds for the east region before 2025-07-01 were
never recorded, and the west region was opened on 2025-10-01.
"""
import datetime as dt
import random
import sqlite3

from mcp.server.mcpserver import MCPServer

REGIONS = ["north", "south", "east", "west"]
PRODUCTS = [  # id, name, category, unit price
    (1, "Ledger Basic", "software", 49.0), (2, "Ledger Pro", "software", 129.0),
    (3, "Scanner S1", "hardware", 310.0), (4, "Scanner S2", "hardware", 540.0),
    (5, "Onboarding", "services", 900.0), (6, "Support Plan", "services", 240.0),
]
START, END = dt.date(2025, 4, 1), dt.date(2026, 9, 30)


def build():
    rnd = random.Random(7)
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript("""
        create table customers (id integer primary key, name text, region text, segment text, signup_date text);
        create table products (id integer primary key, name text, category text, unit_price real);
        create table orders (id integer primary key, order_date text, customer_id integer, product_id integer,
                             qty integer, amount real, status text, refund_amount real);
        create table targets (region text, fiscal_quarter text, target_net_revenue real);
    """)
    db.executemany("insert into products values (?,?,?,?)", PRODUCTS)
    customers = []
    for i in range(1, 81):
        region = rnd.choice(REGIONS)
        first = dt.date(2025, 10, 1) if region == "west" else START
        signup = first + dt.timedelta(days=rnd.randrange((END - first).days - 30))
        customers.append((i, f"Customer {i:03}", region, rnd.choice(["smb"] * 3 + ["enterprise"]), signup.isoformat()))
    db.executemany("insert into customers values (?,?,?,?,?)", customers)

    oid = 0
    for cid, _, region, segment, signup in customers:
        day = dt.date.fromisoformat(signup)
        while True:
            day += dt.timedelta(days=rnd.randrange(12, 70))
            if day > END:
                break
            pid, _, _, price = rnd.choice(PRODUCTS)
            qty = rnd.randint(1, 8 if segment == "enterprise" else 3)
            amount = round(price * qty, 2)
            status = rnd.choices(["completed", "refunded", "cancelled"], [86, 8, 6])[0]
            refund = amount if status == "refunded" else 0.0
            if status == "refunded" and region == "east" and day < dt.date(2025, 7, 1):
                status, refund = "completed", 0.0  # the data-quality gap
            oid += 1
            db.execute("insert into orders values (?,?,?,?,?,?,?,?)",
                       (oid, day.isoformat(), cid, pid, qty, amount, status, refund))

    for fy in (2026, 2027):
        for q in range(1, 5):
            for region in REGIONS:
                if region == "west" and (fy, q) < (2026, 3):
                    continue
                base = {"north": 26000, "south": 18000, "east": 21000, "west": 9000}[region]
                db.execute("insert into targets values (?,?,?)",
                           (region, f"FY{fy}-Q{q}", round(base * (1 + 0.04 * (q - 1)) * (1.1 if fy == 2027 else 1), -2)))
    db.commit()
    db.execute("pragma query_only = on")
    return db


db = build()
mcp = MCPServer("demo-sales")


@mcp.tool()
def list_tables() -> str:
    """List the tables in the sales database with their row counts."""
    names = [r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]
    return "\n".join(f"{n}: {db.execute(f'select count(*) from {n}').fetchone()[0]} rows" for n in names)


@mcp.tool()
def describe_table(table: str) -> str:
    """Show the columns of one table and three sample rows."""
    cols = db.execute("select name, type from pragma_table_info(?)", (table,)).fetchall()
    if not cols:
        return f"No table named {table}."
    rows = db.execute(f'select * from "{table}" limit 3').fetchall()
    return "columns: " + ", ".join(f"{n} {t}" for n, t in cols) + "\nsample:\n" + "\n".join(map(str, rows))


@mcp.tool()
def query(sql: str) -> str:
    """Run a read-only SQL (SQLite) query. Returns at most 200 rows with a header line."""
    try:
        cur = db.execute(sql)
    except sqlite3.Error as e:
        return f"SQL error: {e}"
    rows = cur.fetchmany(201)
    head = " | ".join(d[0] for d in cur.description or [])
    more = "\n(more than 200 rows; aggregate or add a limit)" if len(rows) > 200 else ""
    return head + "\n" + "\n".join(" | ".join(map(str, r)) for r in rows[:200]) + more


if __name__ == "__main__":
    mcp.run()
