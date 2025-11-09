# Execution Engine 使用说明

本文档介绍 `trading/execution.py` 中新增的 `ExecutionConfig` 与 `ExecutionEngine` 的用途、配置方式以及在策略中的集成步骤，帮助你快速启用批量订单调度与自动重试逻辑。

## 功能概览

`ExecutionEngine` 提供以下能力：

- **拆分下单**：将目标数量按配置拆成多笔（默认每笔 1-2 U）顺序提交。
- **可控节奏**：支持在相邻订单之间插入等待时间，避免一次性将全部流动性打空。
- **部分成交处理**：订单允许部分成交，未成交的剩余数量会在下次重试时继续处理。
- **自动重试**：若在设定时间内未成交，会按步长自动调整报价（卖出降低 / 买入抬价）并重新下单，直到达到重试次数上限。
- **GTC/IOC 支持**：默认使用 `GTC` 类型，可根据需求切换为 `IOC`（需在自定义 API 客户端中调整）。
- **统一结果结构**：所有下单调用都返回 `ExecutionResult`，便于策略层判定是否成交、成交数量及最后一次报价。

## 配置参数

所有执行参数集中在 `config/trading.yaml` 中维护，可根据策略需求调整：

| 参数名 | 默认值 | 说明 |
| --- | --- | --- |
| `order_slice_min` | `1.0` | 单笔订单允许的最小数量。 |
| `order_slice_max` | `2.0` | 单笔订单允许的最大数量。 |
| `retry_attempts` | `2` | 价格退让的最大重试次数。实际尝试次数 = `retry_attempts + 1`。 |
| `price_tolerance_step` | `0.0075` | 每次重试时价格退让百分比（0.75%）。 |
| `wait_seconds` | `5.0` | 等待订单成交的时间窗口，同时也是默认的拆单间隔。 |
| `poll_interval_seconds` | `0.5` | 轮询订单成交状态的间隔。 |
| `order_interval_seconds` | `0.0` | 拆单之间的额外延时；若为 `None`/空值，则继承 `wait_seconds`。 |
| `min_quote_amount` | `1.0` | 单笔买单至少投入的美元金额，避免拆分后低于交易所 `$1` 的下限。 |

> **提示**：`ExecutionConfig` 会自动校验参数合法性，例如最小/最大数量必须为正、最大数量需大于最小数量等。

## 典型集成流程

1. **提供 Polymarket API 客户端**
   - 可以直接复用 `trading.execution.ClobPolymarketAPI`，将已有的 `py_clob_client.client.ClobClient` 实例包一层即可。
   - 如需自定义，实现 `PolymarketAPI` 接口，保证 `create_order` 返回包含 `orderId` 的字典，`get_order_status` 则需提供 `status` 与 `filledAmount` 字段。

2. **加载执行配置**
   ```python
   from trading.execution import ExecutionConfig

   exec_config = ExecutionConfig.from_yaml("config/trading.yaml")
   ```

3. **实例化执行引擎**
   ```python
   from trading.execution import ExecutionEngine

   engine = ExecutionEngine(api_client=my_polymarket_client, config=exec_config)
   ```

4. **在策略中触发买/卖单**
   ```python
   from trading.execution import ExecutionResult

   sell_result: ExecutionResult = engine.execute_sell(
       token_id="12345",  # Polymarket tokenId
       price=0.42,
       quantity=15.0,
   )
   if sell_result.status == "FILLED":
       print("全部成交", sell_result.filled)

   buy_result = engine.execute_buy(
       token_id="12345",
       price=0.38,
       quantity=6.0,
   )
   if buy_result.status != "FILLED":
       print("仍有剩余", buy_result.remaining)
   ```

   - 引擎会自动拆分 `quantity`，逐笔提交订单，允许部分成交并在必要时重试。
   - `ExecutionResult` 包含 `filled`、`requested`、`status`、`avg_price`、`limit_price`、`last_price`、`attempts`、`message` 等字段，可直接用于策略判断。

## 自定义与扩展

- **调节拆单节奏**：将 `order_interval_seconds` 设置为正数即可在订单之间强制等待固定时间；若希望立即提交下一笔，设为 `0`。
- **更激进的价格退让**：增大 `price_tolerance_step` 或 `retry_attempts`，即可加快价格跟随速度（卖出向下、买入向上）。
- **自定义等待逻辑**：通过在构造 `ExecutionEngine` 时传入 `clock` 与 `sleep` 回调，可用于测试或与外部调度器集成。
- **切换订单类型**：若需要 `IOC` 或其他 time-in-force，可在自定义 API 客户端中修改 `_create_sell_order` 请求体（`type` 与 `timeInForce` 字段）。
- **避免<$1 买单**：如在低价市场希望继续拆单，可调大 `min_quote_amount` 或临时关闭拆单（提高 `order_slice_max`），确保每笔买单金额符合交易所下限。

## 测试验证

`tests/test_execution.py` 提供了对核心流程的单元测试样例，可作为编写自定义测试或模拟环境的参考：

- 拆单与间隔控制
- 部分成交及剩余数量继续处理
- 超时后的价格退让重试逻辑

执行 `pytest tests/test_execution.py` 即可验证基础功能。

## 常见问题

- **为何没有完全成交？** 可能在达到最大重试次数后仍未成交。可增大 `retry_attempts`、调大价格退让或延长 `wait_seconds`。
- **如何兼容不同币种或单位？** `ExecutionEngine` 默认处理浮点数量，确保调用方传入的价格与数量单位与交易所 API 一致即可。
- **如何在回测中使用？** 提供一个模拟的 `PolymarketAPI` 客户端，实现 `create_order` 与 `get_order_status` 即可复用执行逻辑。
