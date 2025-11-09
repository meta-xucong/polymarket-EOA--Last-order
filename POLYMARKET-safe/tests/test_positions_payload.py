import sys
import types

import pytest


class _RequestException(Exception):
    pass


class _Timeout(_RequestException):
    pass


class _HTTPError(_RequestException):
    pass


def _default_get(*args, **kwargs):  # pragma: no cover - defensive stub
    raise RuntimeError("requests stub should be patched in tests")


requests_stub = types.SimpleNamespace(
    RequestException=_RequestException,
    Timeout=_Timeout,
    HTTPError=_HTTPError,
    get=_default_get,
)

sys.modules.setdefault("requests", requests_stub)


class _WebsocketStub(types.SimpleNamespace):
    def WebSocketApp(self, *args, **kwargs):  # pragma: no cover - defensive stub
        raise RuntimeError("websocket stub should not be used in tests")


sys.modules.setdefault("websocket", _WebsocketStub())

from Volatility_arbitrage_run import (
    _extract_positions_from_data_api_response,
    _fetch_positions_from_data_api,
)


class DummyResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):  # pragma: no cover - triggered only on errors
        if self.status_code >= 400:
            raise _HTTPError(f"status={self.status_code}")


class DummyClient(types.SimpleNamespace):
    pass


def test_extract_positions_handles_various_shapes():
    assert _extract_positions_from_data_api_response(None) == []
    sample_list = [{"asset": "1"}]
    assert _extract_positions_from_data_api_response(sample_list) == sample_list
    sample_dict = {"data": [{"asset": "2"}]}
    assert _extract_positions_from_data_api_response(sample_dict) == [{"asset": "2"}]
    assert _extract_positions_from_data_api_response({"unexpected": []}) is None


def test_fetch_positions_aggregates_pages(monkeypatch):
    module = __import__("Volatility_arbitrage_run")

    first_page = [{"asset": str(i), "size": "1"} for i in range(500)]
    second_page = [{"asset": "500", "size": "2"}]
    responses = [
        DummyResponse(200, {"data": first_page, "meta": {"total": 501}}),
        DummyResponse(200, {"data": second_page, "meta": {"total": 501}}),
    ]
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params or {}), timeout))
        return responses.pop(0)

    monkeypatch.setattr(module.requests, "get", fake_get)

    client = DummyClient(funder="0xabc")
    positions, ok, origin = _fetch_positions_from_data_api(client)
    assert ok is True
    assert origin.startswith("data-api positions(")
    assert len(positions) == 501
    assert calls[0][1]["offset"] == 0
    assert calls[1][1]["offset"] == 500


def test_fetch_positions_missing_address():
    client = DummyClient()
    positions, ok, info = _fetch_positions_from_data_api(client)
    assert positions == []
    assert ok is False
    assert "地址" in info


def test_fetch_positions_handles_http_error(monkeypatch):
    module = __import__("Volatility_arbitrage_run")

    def fake_get(url, params=None, timeout=None):  # pragma: no cover - simple stub
        raise module.requests.Timeout("boom")

    monkeypatch.setattr(module.requests, "get", fake_get)

    client = DummyClient(funder="0xabc")
    positions, ok, info = _fetch_positions_from_data_api(client)
    assert positions == []
    assert ok is False
    assert "请求失败" in info
