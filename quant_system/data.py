import csv
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Union

from .models import Bar


def _parse_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("unsupported date format: %s" % value)


def load_csv(path: Union[str, Path]) -> List[Bar]:
    """Load OHLCV data. Column names are case-insensitive."""
    bars: List[Bar] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header")
        names = {name.lower().strip(): name for name in reader.fieldnames}
        required = {"date", "open", "high", "low", "close", "volume"}
        missing = required - set(names)
        if missing:
            raise ValueError("CSV missing columns: %s" % ", ".join(sorted(missing)))
        for row_number, row in enumerate(reader, start=2):
            try:
                bar = Bar(
                    date=_parse_date(row[names["date"]]),
                    open=float(row[names["open"]]),
                    high=float(row[names["high"]]),
                    low=float(row[names["low"]]),
                    close=float(row[names["close"]]),
                    volume=float(row[names["volume"]]),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid CSV row %d: %s" % (row_number, exc)) from exc
            _validate_bar(bar, row_number)
            bars.append(bar)
    bars.sort(key=lambda item: item.date)
    if len({bar.date for bar in bars}) != len(bars):
        raise ValueError("CSV contains duplicate dates")
    return bars


def _validate_bar(bar: Bar, row_number: int = 0) -> None:
    label = " at row %d" % row_number if row_number else ""
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        raise ValueError("prices must be positive%s" % label)
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
        raise ValueError("invalid OHLC relationship%s" % label)
    if bar.volume < 0:
        raise ValueError("volume must be non-negative%s" % label)


def generate_market_data(
    days: int = 520,
    seed: int = 42,
    start_price: float = 100.0,
    start_date: date = date(2023, 1, 2),
) -> List[Bar]:
    """Generate deterministic business-day OHLCV data with changing regimes."""
    if days < 2 or start_price <= 0:
        raise ValueError("days must be >= 2 and start_price must be positive")
    rng = random.Random(seed)
    result: List[Bar] = []
    current_date = start_date
    previous_close = start_price
    trading_day = 0
    while len(result) < days:
        if current_date.weekday() < 5:
            # Alternating regimes make trend and mean-reversion strategies observable.
            phase = (trading_day // 90) % 4
            drift = (0.0008, -0.00045, 0.00015, 0.00065)[phase]
            cycle = 0.0015 * math.sin(trading_day / 13.0)
            overnight = rng.gauss(0, 0.003)
            daily_return = drift + cycle + rng.gauss(0, 0.011)
            open_price = previous_close * (1 + overnight)
            close = max(1.0, open_price * (1 + daily_return))
            spread = abs(rng.gauss(0.008, 0.003))
            high = max(open_price, close) * (1 + spread)
            low = min(open_price, close) * max(0.01, 1 - spread)
            volume = float(int(800_000 * math.exp(rng.gauss(0, 0.35))))
            result.append(Bar(current_date, open_price, high, low, close, volume))
            previous_close = close
            trading_day += 1
        current_date += timedelta(days=1)
    return result


def closes(bars: Iterable[Bar]) -> List[float]:
    return [bar.close for bar in bars]
