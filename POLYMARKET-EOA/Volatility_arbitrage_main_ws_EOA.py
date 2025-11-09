# Volatility_arbitrage_main_ws_EOA.py
# -*- coding: utf-8 -*-
"""
WS 订阅工具（与 Safe 版功能一致），可与 EOA 交易脚本配套使用。
逻辑保持和原版一致，仅文件命名区分。
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Any, Callable, Dict, List, Optional

try:
    import websocket  # websocket-client
except Exception as exc:  # pragma: no cover - 运行期兜底
    raise RuntimeError("缺少依赖，请先安装： pip install websocket-client") from exc

WS_BASE = "wss://ws-subscriptions-clob.polymarket.com"
CHANNEL = "market"


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")


def ws_watch_by_ids(
    asset_ids: List[str],
    label: str = "",
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    verbose: bool = False,
    stop_event: Optional[threading.Event] = None,
) -> None:
    ids = [str(x) for x in asset_ids if x]
    if not ids:
        raise ValueError("asset_ids 为空")

    if verbose and label:
        print(f"[INIT] 订阅: {label}")
    if verbose:
        for i, tid in enumerate(ids):
            print(f"  - token_id[{i}] = {tid}")

    stop_flag = {"v": False}

    def on_open(ws):
        if verbose:
            print(f"[{_now()}][WS][OPEN] -> {WS_BASE+'/ws/'+CHANNEL}")
        payload = {"type": CHANNEL, "assets_ids": ids}
        ws.send(json.dumps(payload))

        def _ping():
            while not stop_flag["v"]:
                if stop_event and stop_event.is_set():
                    break
                try:
                    ws.send("PING")
                    time.sleep(10)
                except Exception:
                    break

        threading.Thread(target=_ping, daemon=True).start()

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except Exception:
            return

        if on_event is None:
            if verbose:
                print(f"[{_now()}][WS][EVENT] {data}")
            return

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    try:
                        on_event(item)
                    except Exception:
                        pass
        elif isinstance(data, dict):
            try:
                on_event(data)
            except Exception:
                pass

    def on_error(ws, error):
        if verbose:
            print(f"[{_now()}][WS][ERROR] {error}")

    def on_close(ws, status_code, msg):
        stop_flag["v"] = True
        if stop_event:
            stop_event.set()
        if verbose:
            print(f"[{_now()}][WS][CLOSED] {status_code} {msg}")

    headers = [
        "Origin: https://polymarket.com",
        "User-Agent: Mozilla/5.0",
    ]
    wsa = websocket.WebSocketApp(
        WS_BASE + "/ws/" + CHANNEL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        header=headers,
    )

    if stop_event:
        def _watch_stop_event():
            stop_event.wait()
            stop_flag["v"] = True
            try:
                wsa.close()
            except Exception:
                pass

        threading.Thread(target=_watch_stop_event, daemon=True).start()

    wsa.run_forever(
        sslopt={"cert_reqs": ssl.CERT_REQUIRED},
        ping_interval=25,
        ping_timeout=10,
    )


__all__ = ["ws_watch_by_ids"]
