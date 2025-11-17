# Polymarket Claim × 历史持仓融合蓝图 v1.0（EOA 专用）

> 目标：在 **官方数据部分缺失/延迟** 的前提下，
> 利用两套脚本：
> - `history_claims_summary_EOA.py`（claim 视角）
> - `history_positions_summary_EOA.py`（历史持仓 & PnL 视角）  
> 尽量还原每个市场 / 每个 outcome 的 **真实现金流与最终结果**。

---

## 1. 两个脚本各自的职责与优势

### 1.1 `history_claims_summary_EOA.py` —— Claim 视角

**数据来源：** Polymarket Data-API `/activity`。

**主要行为：**

1. 调用 `/activity?user=<wallet>`，本地按 `entry["type"] == "REDEEM"` 过滤：
   - 解决后端 `?type=REDEEM` 过滤器可能不完整的问题。
2. 对每条 REDEEM 记录，读取：
   - `timestamp`
   - `conditionId`
   - `usdcSize`（赎回 USDC 数量）
   - 可选：`asset`（tokenId，可能为空）、`outcomeIndex`、`outcome`、`title`、`slug` 等。
3. 将每条 REDEEM 事件封装为 `Event`，并按「聚合键」做汇总：
   - 每个键下统计：
     - `redeem_size_total`（赎回总份数）
     - `redeem_usdc_total`（赎回总金额，来自 `usdcSize`）
     - `cash_flow_total`（从 claim 视角看，就是赎回收入）
     - `first_ts` / `last_ts`（首次 & 最后一次 claim 时间）

**优点：**

- `/activity` 实时性强，能看到**最近发生的 claim**；
- `usdcSize` 为官方记录，赎回金额**准确可靠**；
- 可以回答：
  - 「哪些 market 在某时间后发生过 claim？」
  - 「每次 claim 的时间 / 份数 / 金额？」
  - 「某个时间段内总共赎回了多少 USDC？」

**缺点：**

- 许多 REDEEM 记录：
  - `asset=""`（没有 tokenId）；
  - `outcome` / `outcomeIndex` 也可能缺失。
- 对这类记录：
  - 无法直接知道是 YES 还是 NO；
  - 无法用 tokenId 与成交记录一一对齐，只能做「按条件 ID 的粗汇总」。

---

### 1.2 `history_positions_summary_EOA.py` —— 历史持仓 & PnL 视角

**数据来源：** `/trades`、`/positions-history`、`/closed-positions` 等历史接口。

**主要行为：**

1. 通过 `/trades` 还原：
   - 每个 token 的所有买入 / 卖出成交；
   - 累计买入数量 / 买入成本；
   - 累计卖出数量 / 卖出收入；
   - 成交价格分布等。
2. 结合 `/positions-history` / `/closed-positions`：
   - 获取市场结算结果（YES / NO）；
   - 尝试区分：OPEN / CLOSED / SETTLED / CLAIMED 等状态；
   - 推出长期 PnL。

**优点：**

- 一旦官方数据补齐，可以得到 **完整 PnL 曲线**；
- tokenId、outcome 信息齐全，能明确区分 YES / NO。

**缺点：**

- `/closed-positions` / `/positions-history` 更新存在延迟或缺失：
  - 刚刚 claim 完的 market，短时间内**可能在这些接口里看不到**；
  - 导致脚本无法识别「已 claim 的最新市场」。

---

## 2. 总体设计思路：用 Claim 视角补全 PnL 视角

核心原则：

1. **Claim 视角是「真实发生了什么」的权威来源。**
   - 哪个 market、在什么时间、claim 了多少 USDC？
   - 完全以 `/activity` 的 `REDEEM` 和 `usdcSize` 为准。
2. **历史持仓视角负责提供「买入 / 卖出 / 理论 PnL」信息。**
   - 能拿到的，就拼进来；
   - 拿不到的，就当成「只有 claim 视角」。

最终想要的对象：

- 对每个 (market, outcome)（或至少 market）：
  - 买入总量、买入成本；
  - 卖出总量、卖出收入；
  - 赎回份数、赎回金额；
  - 最终净现金流（从买入视角：-买入成本 + 卖出收入 + 赎回收入）；
  - 是否有完整的历史成交信息；
  - claim 的时间区间；
  - 标记哪些市场是「数据尚未补全，只能部分还原」。

---

## 3. 聚合键设计：统一两侧的「主键」

要把两份脚本的数据拼接起来，需要一个统一的「键」，用于指认：

> 「**这是哪个 condition 下的哪个 outcome**？」

### 3.1 定义通用 key：`conditionId + outcome`

设计函数：

```python
def make_key(condition_id: str | None,
             outcome_index: int | None,
             outcome: str | None) -> str:
    cid = (condition_id or "").strip()
    label = (outcome or "").strip()

    if not cid:
        # 条件 ID 都没有，只能做降级聚合
        return f"UNKNOWN#{label or outcome_index or 'UNK'}"

    if label:
        # 优先使用 outcome 文本
        return f"{cid}#{label}"

    if outcome_index is not None:
        # 退而求其次，用 outcomeIndex
        return f"{cid}#{outcome_index}"

    return f"{cid}#UNK"
```

统一约定：

- **`history_positions_summary_EOA.py`**：
  - 在内部和 JSON 输出中，为每个 position 生成 `key = make_key(conditionId, outcomeIndex, outcome)`。
- **`history_claims_summary_EOA.py`**：
  - 对每条 REDEEM / TRADE 事件，同样生成 `key`；
  - 聚合时按 `key` 分桶，而不是用原始的 `asset` 字段。

这样，只要 REDEEM 里至少有 `conditionId + outcomeIndex` **或** `conditionId + outcome`，就可以和历史成交对齐。

> 注意：如果 REDEEM 连 `outcomeIndex` 都没有，仍然无法精确判断 YES/NO，
> 这类记录将是不可避免的「模糊区」，只能做 market 级别的粗汇总或单独标注。

---

## 4. JSON 输出规范（为拼接做准备）

### 4.1 `history_positions_summary_EOA.py` 的 JSON 结构

增加 `--json` 参数时，输出类似：

```jsonc
{
  "wallet": "0x...",
  "since_date_utc8": "YYYY-MM-DD",
  "positions": [
    {
      "key": "0xConditionId#No",         // make_key(...) 的结果
      "token_id": "1234567890...",       // 若有
      "condition_id": "0xConditionId",
      "outcome_index": 1,
      "outcome": "No",
      "title": "North Korea missile launch by November 15?",
      "slug": "north-korea-missile-launch-by-november-15-879",
      "side": "NO",                      // 从你持仓视角（Yes/No）
      "buy_size_total": 10.0,
      "buy_cost_total": 9.5,
      "sell_size_total": 0.0,
      "sell_proceeds_total": 0.0,
      "realized_pnl": -9.5,              // 若能推导
      "status": "CLOSED/SETTLED/...",
      "resolved_at": "2025-11-15T...Z",
      "claim_tx": "0x..."                // 若已知
    },
    ...
  ]
}
```

### 4.2 `history_claims_summary_EOA.py` 的 JSON 结构

维持现有 `--json` 输出，但在内部用 `key` 聚合：

```jsonc
{
  "wallet": "0x...",
  "since_date_utc8": "YYYY-MM-DD",
  "filter_by": "redeem",
  "claim_events": [
    {
      "key": "0xConditionId#No",
      "condition_id": "0xConditionId",
      "outcome_index": 1,
      "outcome": "No",
      "title": "North Korea missile launch by November 15?",
      "slug": "north-korea-missile-launch-by-november-15-879",
      "size": 2.0,
      "usdc_size": 2.0,
      "timestamp": 1731782684,
      "tx_hash": "0x..."
    },
    ...
  ],
  "positions": {
    "0xConditionId#No": {
      "key": "0xConditionId#No",
      "condition_id": "0xConditionId",
      "outcome_index": 1,
      "outcome": "No",
      "title": "...",
      "slug": "...",
      "buy_size_total": 10.0,      // 如果能对上 trades，就一并统计；否则为 0
      "buy_cost_total": 9.5,
      "sell_size_total": 0.0,
      "sell_proceeds_total": 0.0,
      "redeem_size_total": 2.0,
      "redeem_usdc_total": 2.0,
      "cash_flow_total": 2.0,
      "first_ts": 1731782684,
      "last_ts": 1731782684
    },
    ...
  }
}
```

在 claim 脚本里：

- 对于能用 `key` 对上的 trades，`buy_*` / `sell_*` 也可以顺带被还原；
- 对于 asset 缺失但 `conditionId + outcomeIndex` 在的 REDEEM，也能做到「按 outcome 粗对齐」。

---

## 5. 融合脚本设计：`history_claims_positions_merged_EOA.py`

### 5.1 输入与输出

**输入：**

1. `positions.json` —— 由 `history_positions_summary_EOA.py --json` 生成；
2. `claims.json`   —— 由 `history_claims_summary_EOA.py --json` 生成。

**输出：**

- 终端打印：按 key / 市场汇总的「综合账本」；
- 支持 `--json` / `--xlsx` 导出：
  - `merged_claims_positions.json`
  - `merged_claims_positions.xlsx`

### 5.2 融合逻辑（伪代码）

```python
pos_data = load_json("positions.json")
claims_data = load_json("claims.json")

pos_by_key = {p["key"]: p for p in pos_data["positions"]}
claim_pos_by_key = {p["key"]: p for p in claims_data["positions"].values()}
claim_events = claims_data["claim_events"]

all_keys = set(pos_by_key) | set(claim_pos_by_key)

merged = []

for key in sorted(all_keys):
    base = pos_by_key.get(key)
    claim_pos = claim_pos_by_key.get(key)

    # 1. 基础信息（title / slug / outcome / side）
    src = base or claim_pos or {}
    title = src.get("title", "")
    slug = src.get("slug", "")
    outcome = src.get("outcome", "")
    side = base.get("side") if base else None

    # 2. 买卖信息（优先使用 positions 视角）
    buy_size = (base or claim_pos or {}).get("buy_size_total", 0.0)
    buy_cost = (base or claim_pos or {}).get("buy_cost_total", 0.0)
    sell_size = (base or claim_pos or {}).get("sell_size_total", 0.0)
    sell_proceeds = (base or claim_pos or {}).get("sell_proceeds_total", 0.0)

    # 3. redeem 信息（只来自 claim 视角）
    redeem_size = (claim_pos or {}).get("redeem_size_total", 0.0)
    redeem_usdc = (claim_pos or {}).get("redeem_usdc_total", 0.0)

    # 4. 现金流：从买入视角（买入为负，卖出 & claim 为正）
    net_cash_flow = -buy_cost + sell_proceeds + redeem_usdc

    # 5. claim 时间区间
    claim_ts_list = [e["timestamp"] for e in claim_events if e["key"] == key]
    first_claim_ts = min(claim_ts_list) if claim_ts_list else None
    last_claim_ts = max(claim_ts_list) if claim_ts_list else None

    merged.append({
        "key": key,
        "title": title,
        "slug": slug,
        "outcome": outcome,
        "side": side,
        "buy_size_total": buy_size,
        "buy_cost_total": buy_cost,
        "sell_size_total": sell_size,
        "sell_proceeds_total": sell_proceeds,
        "redeem_size_total": redeem_size,
        "redeem_usdc_total": redeem_usdc,
        "net_cash_flow": net_cash_flow,
        "has_claim": redeem_usdc > 0,
        "has_position_info": base is not None,
        "first_claim_ts": first_claim_ts,
        "last_claim_ts": last_claim_ts,
    })
```

### 5.3 结果解读

融合结果中，可以轻松区分几类市场：

1. **完全闭环（理想状态）**
   - `has_position_info = True`
   - `has_claim = True`
   - `buy_* / sell_* / redeem_*` 都有值
   - 可以得到完整：买入 → 卖出 → claim → 净现金流

2. **只有持仓视角，没有 claim（尚未赎回 / 输的一侧）**
   - `has_position_info = True`
   - `has_claim = False`
   - 通常是：
     - 还没结算 / 还没 claim；
     - 或者是输的一侧（无 redeem）。

3. **只有 claim 视角，没有持仓视角（最近 claim，positions 数据尚未补全）**
   - `has_position_info = False`
   - `has_claim = True`
   - 代表：
     - `/activity` 已记录 claim；
     - `/closed-positions` / `/positions-history` 尚未更新；
     - 目前只能看到赎回金额，看不到完整买入成本。

对于第 3 类市场，后续如果官方数据补齐，只需要重新跑一遍：

- `history_positions_summary_EOA.py --json`
- `history_claims_summary_EOA.py --json`
- 再用融合脚本 join，原先的「only-claim」市场就会变成「完全闭环」。

---

## 6. 不可避免的「信息黑洞」与应对策略

### 6.1 完全缺失 outcome 信息的 REDEEM

若 REDEEM 记录：

- `conditionId` 有；
- 但 `outcome` / `outcomeIndex` 都缺失；

则 `make_key` 只能生成类似：`0xConditionId#UNK`。

**风险：** 无法保证这笔 claim 属于 YES 还是 NO。

**应对：**

1. 对这类 key：
   - 仍然统计 `redeem_size_total` / `redeem_usdc_total`；
   - 但在打印时明确标记：

     > `[NOTE] 此 REDEEM 记录缺少 outcome 信息，只能按 market 级别粗略统计，无法精确区分 YES/NO。`

2. 若你在某个 condition 下，**只交易过一个 outcome（例如你只买 NO）**：
   - 可选启发式：将 `#UNK` 的 REDEEM 合并到该 outcome 上（带风险）。
   - 建议在脚本中默认保守，不自动合并，只在人工检查时考虑。

### 6.2 官方数据延迟 / 不完整

- 短期内，部分刚 claim 的市场不会出现在 `/closed-positions`：
  - 只能通过 `/activity` 看见 claim；
  - 历史 PnL 的「持仓视角」需要等官方补齐。
- 这类缺口无法通过本地脚本“创作”出来，只能：
  - 先记录为「only-claim」状态；
  - 未来重新同步一轮后补上。

---

## 7. 最终落地建议

1. **保持现有两套脚本的独立性：**
   - `history_claims_summary_EOA.py`：专职处理 claim & activity；
   - `history_positions_summary_EOA.py`：专职处理历史成交 & PnL。
2. **增加统一的 key 机制（make_key）：**
   - 两个脚本内部都用 `key = conditionId + outcome/outcomeIndex`。
3. **为两个脚本都加上 `--json` 输出能力：**
   - 方便后续融合脚本消费。
4. **新建一个融合脚本 `history_claims_positions_merged_EOA.py`：**
   - 输入两份 JSON，生成最终「综合账本」；
   - 支持控制台打印 + JSON 导出 + Excel 导出。
5. **长远：**
   - 随着 Polymarket 官方 Data-API 补全：
     - 这套「融合蓝图」可以自然过渡到更精确的 PnL 统计；
     - 同时保留 activity 视角，用于审计「链上实际赎回」与「历史 PnL 推导」的一致性。

---

> 结论：  
> **在官方历史接口不完全可靠的前提下，
> 以 `history_claims_summary_EOA.py` 作为「claim 事实」的权威来源，
> 再用 `history_positions_summary_EOA.py` 尽力还原买卖与 PnL，
> 通过统一的 key 将两者拼起来，是当前阶段最稳妥的解决方案。**

