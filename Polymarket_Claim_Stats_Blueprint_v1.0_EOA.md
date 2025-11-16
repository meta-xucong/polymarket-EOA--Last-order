
# Polymarket Claim 统计蓝图 v1.0（EOA 专用）

## 0. 目标与原则

**目标：**

1. 能列出「从某个日期起，发生过哪些 REDEEM / Claim」，包括：
   - 哪个市场、哪个 outcome、什么时间；
   - 赎回了多少份、拿回多少 USDC（按 REDEEM 的 `usdcSize`）；
2. 在此基础上，**用“现金流口径”算一遍真实 PnL**：
   - 买入：USDC 流出  
   - 卖出：USDC 流入  
   - REDEEM：USDC 流入  
   → per token / per condition，汇总成 realized PnL（现金口径）。

**原则：**

- **Claim 视角和做单视角解耦**：  
  不再指望 `/closed-positions` 告诉你 claim 明细；  
  - *做单统计* 继续用历史脚本；  
  - *claim 统计* 专门用 `/activity?type=REDEEM` + `/trades` 做现金流。
- **时间窗口按需求区分：**
  - 视图 A：按「Buy 时间」看这段时间新开的仓位表现（现在脚本做的）。  
  - 视图 B：按「Redeem 时间」看这段时间做了哪些 claim（本蓝图要做的）。

---

## 1. 数据源设计

### 1.1 Data-API `/trades`（成交流）

- 作用：提供 BUY/SELL 现金流和持仓成本。
- 关键字段：
  - `asset` / `tokenId`
  - `side`：BUY / SELL
  - `size`：份数
  - `price`：单价
  - `timestamp`
  - `conditionId` / `title` / `slug` / `outcome` 等辅助信息

### 1.2 Data-API `/activity`，**type = REDEEM**

- 作用：提供每一次 redeem 的现金流。
- 调用方式：
  - `GET /activity?user=<EOA or proxyWallet>&type=REDEEM&limit=...&cursor=...`
- 关键字段（按官方文档）：
  - `asset`：tokenId
  - `conditionId`
  - `timestamp`
  - `type = "REDEEM"`
  - `size`：赎回的份数
  - `usdcSize`：这次 redeem 收到的 USDC 数量
  - `outcomeIndex` / `outcome`
  - `title` / `slug` / `eventSlug`（用于展示）

### 1.3 Data-API `/closed-positions`（可选）

- 作用：作为**官方 realizedPnl 的标尺**，用来对照现金流口径是否大致一致。
- 注意：  
  不依赖它给 claim 细节，纯粹作为校验 / 对比。

### 1.4 Gamma `/markets`（可选）

- 作用：
  - 拿 `outcomes` + `outcomePrices` + `winningOutcome` 识别「最终赢家」；
  - 给市场加上一些元信息（问题标题、slug、结束时间）。

这部分逻辑可以直接复用你现在脚本里的 `_lookup_markets_for_assets` + `_resolve_token_meta`，但在 claim 蓝图里是「可选增强」，不是硬依赖。

---

## 2. 数据模型：三层结构

### 2.1 Event 层（原始事件）

建立一个统一的内部事件结构，三种类型：

1. **TRADE_BUY**
   - key 字段：
     - `asset` (tokenId)
     - `conditionId`
     - `timestamp`
   - 现金流：`cash = - size * price`（USDC 流出）
2. **TRADE_SELL**
   - `cash = + size * price`（USDC 流入）
3. **REDEEM**
   - 从 `/activity?type=REDEEM` 来：
   - 现金流：
     - 主口径：`cash = + usdcSize`  
     - 辅助：记录 `redeem_size = size`（这次赎回的份数，主要用于对照）

所有事件统一成结构，比如（概念上）：

```text
{
  asset: token_id,
  conditionId: ...,
  outcomeIndex: ...,
  type: TRADE_BUY / TRADE_SELL / REDEEM,
  size: float,
  price: float | None,
  usdcSize: float | None,
  cash: float,  # 买卖 / redeem 的 USDC 变动
  timestamp: float
}
```

### 2.2 Position 层（按 asset 聚合）

按 `asset`（必要时再加上 `conditionId + outcomeIndex`）聚合，构建一个**PositionSummary**：

字段建议：

- 标识部分：
  - `asset` / `conditionId` / `outcomeIndex`
  - `title` / `slug` / `outcomeLabel`（从 trades 或 activity 任一来源拿）
- 持仓与成本：
  - `buy_size_total`：∑ BUY size
  - `buy_cost_total`：∑ BUY size × price（正数）
  - `sell_size_total`：∑ SELL size
  - `sell_proceeds_total`：∑ SELL size × price（USDC 流入）
- redeem：
  - `redeem_size_total`：∑ REDEEM size
  - `redeem_usdc_total`：∑ REDEEM usdcSize
  - `first_redeem_ts` / `last_redeem_ts`
- 现金流：
  - `cash_flow_total = -buy_cost_total + sell_proceeds_total + redeem_usdc_total`
  - `realized_pnl_cash = cash_flow_total`（如果你只看这条 token 的单边 PnL）

> 注意：这里 deliberately 不做「份数上是否完全对冲」的复杂追踪（例如先卖后 redeem等）——先按「现金流」简单求和，这是最直观也最稳定的口径，后面再考虑高级匹配。

### 2.3 Market 层（按 condition 聚合 / 报表）

按 `conditionId` 聚成「一个市场」：

- `market_title`
- `market_slug`
- `positions`: [PositionSummary...]
- `market_pnl_cash = ∑ position.realized_pnl_cash`
- 可选：`market_winner_outcome`（通过 Gamma 推出来）

---

## 3. 查询维度与输出视图

### 3.1 视图 A：Claim 事件明细（按时间排序）

用于回答：

> “从 2025-11-13 开始，我在 Polymarket 一共 claim 了哪些市场，每次拿了多少钱？”

流程：

1. 从 `/activity?type=REDEEM` 拉全部数据，过滤 `timestamp >= since_ts`；
2. 每条 REDEEM 事件，直接以一行形式输出：

   - 市场标题（`title` / Gamma）
   - outcome 名称（`outcome` / `outcomeIndex`）
   - `size`、`usdcSize`
   - `timestamp`（转为 UTC+8）
   - `asset` / `conditionId`
   - 对应的 BUY 均价（如果能从 trades 找到的话）——可选加一列 “大致盈亏 = usdcSize - 已卖出 + 未卖出成本”，但这属于进阶。

这个视图完全不依赖你有没有设定某个 since_date 作为买入时间，只看 “Redeem 时间”。

### 3.2 视图 B：按 token 的 Claim + 现金流 PnL

用于回答：

> “对每一条 token（例如某个 market 的 Yes/No），从头到尾买卖 + redeem 加在一起，我赚了还是亏了？”

输出每个 PositionSummary：

- 标题行：
  - `<title> | <outcomeLabel> | token_id=...`
- 买卖概览：
  - `买入总量/均价/总成本`
  - `卖出总量/总收入`
- claim 概览：
  - `redeem_size_total` / `redeem_usdc_total`
  - `第一次 redeem 时间 -> 最后一次 redeem 时间`
- 现金流 PnL：
  - `cash_flow_total`（核心数字）
  - 如果结合 Gamma winner 信息，可再标一列 “最终赢家 = Yes/No/xxx”。

**注意**：  
这里的 time filter 可以有两种模式：

1. **按买入时间过滤**（类似你现在脚本）：
   - 只统计 since_date 之后新开的仓位；
2. **按 redeem 时间过滤**（更适合 claim 视角）：
   - 只统计 since_date 之后发生过 redeem 的仓位。

蓝图建议：提供一个 `--filter-by` 参数，`trade` / `redeem` 二选一，或者脚本专门写成 “Claim 统计版”，就固定用 `redeem` 口径。

### 3.3 视图 C：按市场汇总

按 `conditionId` 聚：

- 对应 market：
  - `title`, `slug`
  - `winner_outcome`（Gamma 推的）
- 统计：
  - `总买入成本`
  - `总卖出收入`
  - `总 redeem 收入`
  - `market_pnl_cash`

适合看「某个事件整体赚了还是亏了」。

---

## 4. 与现有 `history_positions_summary_EOA.py` 的关系

为了避免搞乱目前已经调通的逻辑，更推荐 **新建一个脚本**，例如：

- `history_claims_summary_EOA.py`  

复用你现有脚本里的这些组件：

- 钱包检测：`_vp_infer_wallet_address`
- 时间处理：`_prompt_since_date` / `_normalize_timestamp` / `_fmt_timestamp_local`
- 市场元数据：`_lookup_markets_for_assets` / `_resolve_token_meta`

**不同点在于**：

1. 数据入口：
   - 旧脚本：`/trades + /activity + /closed-positions` 然后“只统计 BUY positions”的表现；
   - 新脚本（本蓝图）：  
     `/trades + /activity(type=REDEEM)`，对所有资产做现金流汇总 + claim 列表。

2. 汇总逻辑：
   - 旧脚本：按「买入份数 × 1 或 × 0」的理论赔付做“推导盈亏”（目前你已经修正赢家判定）。
   - 新脚本：完全基于真实 cash flow：
     - 买：`-size * price`  
     - 卖：`+size * price`  
     - redeem：`+usdcSize`

---

## 5. 自检 checklist（验证这套逻辑不是“掩盖问题”）

实现新脚本后，可以按下面步骤自检：

1. 任意挑一笔刚 claim 的市场：
   - 用 `/activity?type=REDEEM` 手动 curl，看是否有这条记录（asset + usdcSize 正确）。
   - 新脚本的 Claim 明细视图里，必须出现完全一致的一行。

2. 对某个简单市场做“手算”验证：
   - 例如：买 2 份 @0.4，之后没卖出，最后 REDEEM 一次拿回 2 USDC。
   - 现金流应为：`-0.8 + 2.0 = +1.2`  
   - 脚本里 `cash_flow_total` 必须是 `+1.2`，且与 `/closed-positions.realizedPnl` 大致接近（考虑手续费轻微差异）。

3. 对一个「只买不 redeem」的市场：
   - `/activity` 没有 REDEEM 记录；
   - 新 claim 视图中不会出现；
   - 现金流 PnL = 只考虑 buy/sell（如果没卖出，应该是纯成本）。

4. 对一个「卖出完，再 redeem 0」的市场：
   - `/activity` 可能有 REDEEM size=0 或根本不出现；
   - 现金流 PnL 主要由买卖决定；
   - claim 视图不应该虚构任何收入。

只要这几条都对得上，你就可以比较放心：  
**新蓝图确实是在吃官方暴露出来的真实 Claim 数据，而不是在“猜结果 + 掩盖问题”。**
