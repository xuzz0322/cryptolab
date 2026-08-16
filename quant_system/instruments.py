from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Dict


@dataclass(frozen=True)
class Instrument:
    symbol: str
    venue: str
    base_currency: str
    quote_currency: str
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal

    def normalize_price(self, price: float) -> float:
        value = Decimal(str(price))
        units = (value / self.price_tick).to_integral_value(rounding=ROUND_DOWN)
        return float(units * self.price_tick)

    def normalize_quantity(self, quantity: float) -> float:
        value = Decimal(str(max(quantity, 0.0)))
        units = (value / self.quantity_step).to_integral_value(rounding=ROUND_DOWN)
        return float(units * self.quantity_step)


INSTRUMENTS: Dict[str, Instrument] = {
    "BTC/USDT": Instrument(
        "BTC/USDT", "BINANCE", "BTC", "USDT", Decimal("0.01"), Decimal("0.00001"), Decimal("0.00001"), Decimal("5")
    ),
    "ETH/USDT": Instrument(
        "ETH/USDT", "BINANCE", "ETH", "USDT", Decimal("0.01"), Decimal("0.0001"), Decimal("0.0001"), Decimal("5")
    ),
    "SOL/USDT": Instrument(
        "SOL/USDT", "BINANCE", "SOL", "USDT", Decimal("0.001"), Decimal("0.001"), Decimal("0.001"), Decimal("5")
    ),
}


def get_instrument(symbol: str) -> Instrument:
    normalized = symbol.upper().replace("-", "/").replace("_", "/")
    if normalized not in INSTRUMENTS:
        raise ValueError("未知币对 %s，请先在 INSTRUMENTS 中配置交易精度" % symbol)
    return INSTRUMENTS[normalized]

