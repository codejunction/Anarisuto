"""Fiscal calendar tools. The fiscal year starts on 1 April and is named after the year it ends."""
import datetime


def quarter_of(day: datetime.date) -> str:
    fy = day.year + 1 if day.month >= 4 else day.year
    q = (day.month - 4) % 12 // 3 + 1
    return f"FY{fy}-Q{q}"


def bounds(fiscal_quarter: str) -> tuple[datetime.date, datetime.date]:
    fy, q = fiscal_quarter.upper().removeprefix("FY").split("-Q")
    if not 1 <= int(q) <= 4:
        raise ValueError(fiscal_quarter)
    first = lambda n: datetime.date(int(fy) - 1 + (n >= 9), (3 + n) % 12 + 1, 1)  # n = months after March
    start = first((int(q) - 1) * 3)
    nxt = (int(q)) * 3
    end = (first(nxt) if nxt < 12 else datetime.date(int(fy), 4, 1)) - datetime.timedelta(days=1)
    return start, end


def fiscal_period(value: str) -> str:
    """Convert between dates and fiscal quarters. Give a date (2026-08-15) to get its fiscal
    quarter, or a fiscal quarter (FY2027-Q2) to get its first and last dates."""
    value = value.strip()
    try:
        if value.upper().startswith("FY"):
            start, end = bounds(value)
            return f"{value.upper()} runs from {start} to {end} inclusive."
        return f"{value} is in {quarter_of(datetime.date.fromisoformat(value))}."
    except ValueError:
        return "Give a date like 2026-08-15 or a fiscal quarter like FY2027-Q2."


def register(harness):
    harness.tools.append(fiscal_period)


if __name__ == "__main__":
    assert quarter_of(datetime.date(2026, 8, 15)) == "FY2027-Q2"
    assert quarter_of(datetime.date(2026, 3, 31)) == "FY2026-Q4"
    assert quarter_of(datetime.date(2025, 4, 1)) == "FY2026-Q1"
    assert bounds("FY2027-Q2") == (datetime.date(2026, 7, 1), datetime.date(2026, 9, 30))
    assert bounds("FY2026-Q4") == (datetime.date(2026, 1, 1), datetime.date(2026, 3, 31))
    assert bounds("fy2026-q3") == (datetime.date(2025, 10, 1), datetime.date(2025, 12, 31))
    print("ok")
