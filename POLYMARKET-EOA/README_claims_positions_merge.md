# Claim × 历史仓位融合脚本使用说明（EOA）

> 适用于 `history_claims_summary_EOA.py`、`history_positions_summary_EOA.py`
> 与 `history_claims_positions_merged_EOA.py` 组合使用的场景。

## 准备环境
- Python 3.8+。
- 若需要导出 Excel，请安装 `openpyxl`：
  ```bash
  pip install openpyxl
  ```

## 快速步骤
1. **生成历史成交 / 仓位 JSON**
   ```bash
   python3 history_positions_summary_EOA.py --json positions.json
   ```
2. **生成 claim JSON（可限定起始日期）**
   ```bash
   python3 history_claims_summary_EOA.py --json claims.json --since "2024-10-01"
   ```
3. **融合并导出**
   ```bash
   python3 history_claims_positions_merged_EOA.py \
     --positions-json positions.json \
     --claims-json claims.json \
     --json-out merged.json \
     --xlsx-out merged.xlsx
   ```

## 参数说明
- `history_positions_summary_EOA.py`
  - `--since YYYY-MM-DD`：按 UTC+8 起始日期过滤成交。
  - `--json [path]`：以 JSON 输出；缺省路径时输出到标准输出，指定路径会写入文件。
- `history_claims_summary_EOA.py`
  - `--since YYYY-MM-DD`：按 UTC+8 起始日期筛选 claim 事件。
  - `--filter-by redeem|trade|all`：默认 `redeem`，如需调试成交数据可选 `trade`。
  - `--json [path]`：以 JSON 输出；缺省路径时输出到标准输出，指定路径会写入文件。
- `history_claims_positions_merged_EOA.py`
  - `--positions-json <path>`：指向上一步生成的 positions JSON（默认脚本同目录下的 `positions.json`）。
  - `--claims-json <path>`：指向上一步生成的 claims JSON（默认脚本同目录下的 `claims.json`）。
  - `--json-out <path>` / `--xlsx-out <path>`：可选导出路径。

## 常见问题
- **直接运行融合脚本报 `未找到文件：positions.json`**：
  先按上面的第 1、2 步生成两份 JSON，再重跑融合脚本；或者在参数中显式指定文件路径。
- **只看到某些 market 没有成交数据，但有 redeem 金额**：
  这表示 Data-API 的某些 REDEEM 记录缺少 tokenId/outcome，脚本会按市场级别粗聚合并在输出中标记。

