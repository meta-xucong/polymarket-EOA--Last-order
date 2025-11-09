# Polymarket Volatility Arbitrage 脚本说明

## 概览

本仓库围绕 `Volatility_arbitrage_run.py` 所实现的波动套利策略构建，提供了行情订阅、策略执行以及下单工具等功能模块。通过这些脚本，可以实现“行情监听 → 跌幅触发买入 → 成交确认 → 盈利卖出 → 回到监听”的循环套利流程，并支持独立复用行情或下单功能。

## 交易逻辑框架

### 核心入口：`Volatility_arbitrage_run.py`
- 首先尝试通过 WebSocket 方式的 `get_client` 构造 CLOB 客户端，失败则回退到 REST 版本。
- 根据用户输入的事件/市场 URL 或手动 `tokenId` 解析目标交易对，再询问交易方向（YES/NO）及策略参数（跌幅窗口、跌幅阈值、盈利目标、触发价等）。
- 将参数封装进 `StrategyConfig` 后交由策略状态机处理，行情通过 `ws_watch_by_ids` 异步推送。
- 策略发出的买卖信号进入队列，由主循环串联买单/卖单执行器，并根据成交或拒单回调推进状态；支持命令行 `stop`、市场关闭检测等安全退出机制。

### 策略状态机：`Volatility_arbitrage_strategy.py`
- 在“空仓（FLAT）”状态下维护一个滚动窗口，跟踪窗口内最高价；当最新买价跌幅超过 `drop_pct` 或触及手动阈值时发出买入信号。
- 在“持仓（LONG）”状态时，若当前卖价达到开仓价乘以盈利系数，则发出卖出信号。
- 所有信号均进入“待确认”状态；只有上游确认成交（`on_buy_filled` / `on_sell_filled`）后才切换状态，拒单则通过 `on_reject` 解除等待，确保买卖流程闭环。
- 额外记录最近行情、持仓规模以及最后一次拒单原因，便于调试。

### 下单执行器
- **买单执行器 `execute_auto_buy`**：自动根据配置拆单、允许部分成交，并在未成交时按步长抬价重试，返回统一的 `ExecutionResult`。
- **卖单执行器 `execute_auto_sell`**：数量向下取整到两位小数后交给批量调度器处理，可在设定次数内逐步降价，最终同样返回 `ExecutionResult`。
- 所有订单均使用 `GTC` 语义（允许部分成交），并通过 `config/trading.yaml` 配置拆单规模、重试次数、价格退让步长等参数；新增的 `min_quote_amount` 可避免买单拆分后低于交易所 $1 的下限。

### 行情与账号支撑模块
- `Volatility_arbitrage_main_ws.py`：薄封装的行情订阅器，连接官方 WebSocket，订阅指定 `tokenIds` 并将解析后的事件回传给上层；调试模式下可通过 REST 解析市场 slug。
- `Volatility_arbitrage_main_rest.py`：读取环境变量 `POLY_KEY`、`POLY_FUNDER` 等，初始化 `ClobClient` 并缓存为单例，供其它脚本共享。
- `Volatility_arbitrage_price_watch.py`：提供纯行情监控功能，解析 `tokenIds` 后复用 WebSocket 订阅，每隔指定秒数打印 YES/NO 买卖价和最新成交价，可作为策略参数调试或外部监控工具。

## 使用指南

### 准备依赖与环境变量
1. 安装必要的第三方库：`py_clob_client`、`websocket-client`、`requests` 等。
2. 设置账户相关环境变量：
   - `POLY_KEY`：十六进制私钥（`0x` 前缀可选）。
   - `POLY_FUNDER`：充值地址。
   - 可选项 `POLY_HOST`、`POLY_CHAIN_ID`、`POLY_SIGNATURE` 用于自定义节点或签名。
3. 之后可在任意脚本中通过 `from Volatility_arbitrage_main_rest import get_client` 复用同一客户端实例。

### 运行自动循环策略：`Volatility_arbitrage_run.py`
1. 执行 `python Volatility_arbitrage_run.py`。
2. 根据提示输入目标市场：支持事件页/市场页 URL 或直接输入 `YES_id,NO_id`。
3. 选择交易方向（YES/NO），配置可选的买入份数、跌幅窗口（分钟）、跌幅阈值（%）、盈利目标（%）以及触发价。
4. 程序开始监听行情；满足条件时自动调用批量执行引擎拆单下单，成交后进入持仓，并在盈利目标达成时触发带价格退让的卖出重试。命令行输入 `stop`/`exit` 可随时终止，脚本也会在检测到市场关闭后自动退出。

### 只看行情：`Volatility_arbitrage_price_watch.py`
- 命令：`python Volatility_arbitrage_price_watch.py --source <url 或 YES_id,NO_id> --interval 1`。
- 解析 `tokenIds` 并订阅行情，每隔指定秒数打印 YES/NO 的价格、买卖盘与最新成交价，适合观察波动或在调整策略参数前验证。按 `Ctrl+C` 即可退出。

### 直接调用下单执行器
- 在自定义策略或单次交易脚本中导入并调用：
  - `execute_auto_buy(client, token_id, price, size)`
  - `execute_auto_sell(client, token_id, price, size)`
- 函数返回 `ExecutionResult`，包含 `status`、`filled`、`remaining`、`avg_price`、`limit_price`、`last_price` 等字段，可快速集成到其它交易逻辑中。

### WebSocket 调试：`Volatility_arbitrage_main_ws.py`
- 命令：`python Volatility_arbitrage_main_ws.py --source <url 或 YES,NO>`。
- 连接官方 WebSocket 并打印完整事件，适合调试数据格式或接入自定义事件处理器。

---

如需进一步扩展，可在现有模块基础上编写自定义策略或风控逻辑，复用行情订阅与下单执行组件即可快速搭建策略原型。
