from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

from .order_models import Fill, Side


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    available_quantity: float = 0.0
    average_cost: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price


class Portfolio:
    """Cash and position ledger with A-share-style T+1 sell availability."""

    def __init__(self, initial_cash: float, settlement_days: int = 0):
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        self.initial_cash = initial_cash
        self.settlement_days = settlement_days
        self.cash = initial_cash
        self.positions: Dict[str, Position] = {}
        self.current_date: Optional[date] = None
        self.day_start_equity = initial_cash
        self.peak_equity = initial_cash

    def start_day(self, trading_date: date, prices: Dict[str, float]) -> None:
        if self.current_date != trading_date:
            for position in self.positions.values():
                position.available_quantity = position.quantity
            self.current_date = trading_date
            self.mark(prices)
            self.day_start_equity = self.equity

    def position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]

    def mark(self, prices: Dict[str, float]) -> None:
        for symbol, price in prices.items():
            if symbol in self.positions:
                self.positions[symbol].last_price = price
        self.peak_equity = max(self.peak_equity, self.equity)

    @property
    def equity(self) -> float:
        return self.cash + sum(position.market_value for position in self.positions.values())

    @property
    def drawdown(self) -> float:
        return self.equity / self.peak_equity - 1 if self.peak_equity else 0.0

    @property
    def daily_return(self) -> float:
        return self.equity / self.day_start_equity - 1 if self.day_start_equity else 0.0

    def apply_fill(self, fill: Fill) -> float:
        position = self.position(fill.symbol)
        position.last_price = fill.price
        if fill.side == Side.BUY:
            cost = fill.quantity * fill.price + fill.commission
            if cost > self.cash + 1e-8:
                raise ValueError("fill cost exceeds available cash")
            previous_cost = position.quantity * position.average_cost
            self.cash -= cost
            position.quantity += fill.quantity
            if self.settlement_days == 0:
                position.available_quantity += fill.quantity
            position.average_cost = (previous_cost + cost) / position.quantity
            return 0.0
        if fill.quantity > position.available_quantity + 1e-12:
            raise ValueError("sell fill exceeds available quantity")
        proceeds = fill.quantity * fill.price - fill.commission
        realized = fill.quantity * (fill.price - position.average_cost) - fill.commission
        self.cash += proceeds
        position.quantity -= fill.quantity
        position.available_quantity -= fill.quantity
        position.realized_pnl += realized
        if abs(position.quantity) < 1e-12:
            position.quantity = 0.0
            position.available_quantity = 0.0
            position.average_cost = 0.0
        return realized
