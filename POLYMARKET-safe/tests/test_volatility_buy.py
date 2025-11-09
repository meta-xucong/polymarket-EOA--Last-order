import math
from types import SimpleNamespace

import pytest

import Volatility_buy as vb
from trading.execution import ExecutionResult


class DummyEngine:
    def __init__(self):
        self.config = SimpleNamespace(
            order_slice_min=1.0,
            order_slice_max=2.0,
            min_quote_amount=1.0,
            min_market_order_size=0.0,
        )
        self.executed = False
        self.last_args = None
        self.min_quote_during_execute = None

    def _slice_quantities(self, total, side=None, price=None):
        min_size = self.config.order_slice_min
        max_size = max(self.config.order_slice_max, min_size)
        if total <= max_size or total < min_size * 2:
            return [total]
        slices = []
        remaining = total
        while remaining > 1e-9:
            if remaining <= max_size:
                if remaining < min_size and slices:
                    slices[-1] += remaining
                else:
                    slices.append(remaining)
                break
            next_qty = min(max_size, remaining - min_size)
            next_qty = max(min_size, next_qty)
            if next_qty > remaining:
                next_qty = remaining
            slices.append(next_qty)
            remaining -= next_qty
        return slices

    def _minimum_buy_size(self, price):
        base_min = max(self.config.order_slice_min, self.config.min_market_order_size)
        if price <= 0 or self.config.min_quote_amount <= 0:
            return base_min
        return max(
            base_min,
            math.ceil((self.config.min_quote_amount / price - 1e-12) * 10000) / 10000,
        )

    def execute_buy(self, token_id, price, quantity):
        self.executed = True
        self.last_args = (token_id, price, quantity)
        self.min_quote_during_execute = self.config.min_quote_amount
        return ExecutionResult(
            side="buy",
            requested=float(quantity),
            filled=float(quantity),
            last_price=float(price),
            attempts=1,
            status="FILLED",
        )


class DummyClient:
    pass


def test_execute_auto_buy_skips_when_balance_insufficient(monkeypatch):
    engine = DummyEngine()
    monkeypatch.setattr(vb, "_build_engine", lambda client: engine)
    monkeypatch.setattr(vb, "_fetch_available_quote_balance", lambda client: 2.0)

    result = vb.execute_auto_buy(DummyClient(), token_id="tkn", price=0.6, size=5.0)

    assert isinstance(result, ExecutionResult)
    assert result.status == "SKIPPED"
    assert engine.executed is False


def test_execute_auto_buy_proceeds_with_sufficient_balance(monkeypatch):
    engine = DummyEngine()
    monkeypatch.setattr(vb, "_build_engine", lambda client: engine)
    monkeypatch.setattr(vb, "_fetch_available_quote_balance", lambda client: 50.0)
    monkeypatch.setattr(vb, "_fetch_best_ask_price", lambda client, token_id: None)

    result = vb.execute_auto_buy(DummyClient(), token_id="tkn", price=0.6, size=3.0)

    assert isinstance(result, ExecutionResult)
    assert result.status == "FILLED"
    assert result.filled == pytest.approx(result.requested)
    assert engine.executed is True
    assert engine.last_args is not None
    assert engine.last_args[0] == "tkn"
    assert engine.last_args[1] == pytest.approx(0.6)
    assert engine.last_args[2] == pytest.approx(3.0)
    assert engine.min_quote_during_execute == pytest.approx(1.02)


def test_execute_auto_buy_expands_min_quote_when_market_price_lower(monkeypatch):
    engine = DummyEngine()
    monkeypatch.setattr(vb, "_build_engine", lambda client: engine)
    monkeypatch.setattr(vb, "_fetch_available_quote_balance", lambda client: 100.0)
    monkeypatch.setattr(vb, "_fetch_best_ask_price", lambda client, token_id: 0.3299)

    result = vb.execute_auto_buy(DummyClient(), token_id="tkn", price=0.33, size=20.0)

    assert isinstance(result, ExecutionResult)
    assert result.status == "FILLED"
    assert engine.executed is True
    assert engine.last_args is not None
    assert engine.last_args[1] == pytest.approx(0.33)
    assert engine.last_args[2] == pytest.approx(20.0)
    assert engine.min_quote_during_execute is not None
    assert engine.min_quote_during_execute == pytest.approx(1.02)
    assert engine.config.min_quote_amount == pytest.approx(1.0)
