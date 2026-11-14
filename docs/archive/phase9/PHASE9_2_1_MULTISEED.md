# Phase 9.2.1 multi-seed live evaluation

Live GLM calls only. These results are reported per seed and are not combined with the 92-case full regression.

| Seed | Cases | Passed | Pass rate | Task completion | Provider failures | Budget stops | Model calls | Tool calls | Input tokens | Output tokens | Wall-clock s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 7 | 22 | 14 | 63.64% | 90.91% | 2 | 0 | 54 | 30 | 366637 | 20621 | 1334.9 |
| 42 | 22 | 12 | 54.55% | 72.73% | 5 | 1 | 50 | 28 | 331978 | 24406 | 1238.37 |
| 123 | 22 | 16 | 72.73% | 100.00% | 0 | 0 | 56 | 33 | 409732 | 25031 | 1181.13 |

Tool-call sequences compared across seeds: 24 cases; 11 differed between at least two seeds. A sequence includes each call's arguments, not just the tool name. A case with a provider or budget failure can differ because one seed never reached that step, so read differences together with the per-seed failure lists.

Cases whose tool calls differed:

- `live-001`
  - seed 7: query_metric{"end": "2025-12-01", "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}], "g...
  - seed 42: query_metric{"end": "2025-12-01", "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}], "m...
  - seed 123: query_metric{"end": "2025-12-01", "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}], "g...
- `live-004`
  - seed 7: compare_periods{"filters": [{"dimension": "region", "operator": "eq", "value": "EC"}], "kind": "mom", "metr... -> top_n{"dimension": "product", "end": "2025-12-01", "filters": [{"dimension": "region", "operator": "eq", "v... -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu... -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu... -> compare_periods{"filters": [{"dimension": "region", "operator": "eq", "value": "EC"}], "kind": "mom", "metr... -> top_n{"dimension": "product", "end": "2025-12-01", "filters": [{"dimension": "product", "operator": "in", "...
  - seed 42: compare_periods{"filters": [{"dimension": "region", "operator": "eq", "value": "EC"}], "kind": "mom", "metr... -> top_n{"dimension": "product", "end": "2025-12-01", "filters": [{"dimension": "region", "operator": "eq", "v... -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu... -> query_metric{"dimensions": ["product"], "end": "2025-12-01", "filters": [{"dimension": "region", "operator"... -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu...
  - seed 123: compare_periods{"filters": [{"dimension": "region", "operator": "eq", "value": "EC"}], "kind": "mom", "metr... -> compare_periods{"filters": [{"dimension": "region", "operator": "eq", "value": "EC"}], "kind": "mom", "metr... -> top_n{"dimension": "product", "end": "2025-12-01", "filters": [{"dimension": "region", "operator": "eq", "v... -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu...
- `live-012`
  - seed 7: compare_periods{"kind": "mom", "metric": "sales_amount", "period": "2025-12-01"} -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu...
  - seed 42: compare_periods{"kind": "mom", "metric": "sales_amount", "period": "2025-12-01"}
  - seed 123: compare_periods{"kind": "mom", "metric": "sales_amount", "period": "2025-12-01"} -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu...
- `live-016`
  - seed 7: anomaly_scan{"end": "2025-12-01", "method": "mom_threshold", "metric": "sales_amount", "start": "2025-01-01...
  - seed 42: (no tool call)
  - seed 123: anomaly_scan{"end": "2025-12-01", "method": "mom_threshold", "metric": "sales_amount", "start": "2025-01-01...
- `phase7-019`
  - seed 7: query_metric{"end": "2025-12-01", "grain": "total", "metric": "net_profit", "start": "2025-12-01"}
  - seed 42: (no tool call)
  - seed 123: query_metric{"end": "2025-12-01", "metric": "net_profit", "start": "2025-12-01"}
- `phase7-044`
  - seed 7: top_n{"ascending": true, "dimension": "region", "end": "2025-12-01", "metric": "gross_margin", "n": 4, "sta... -> top_n{"ascending": false, "dimension": "region", "end": "2025-12-01", "metric": "stockout_rate", "n": 4, "s...
  - seed 42: (no tool call)
  - seed 123: top_n{"ascending": true, "dimension": "region", "end": "2025-12-01", "metric": "gross_margin", "n": 4, "sta... -> top_n{"ascending": false, "dimension": "region", "end": "2025-12-01", "metric": "stockout_rate", "n": 4, "s...
- `phase7-052`
  - seed 7: list_metrics
  - seed 42: list_metrics -> describe_metric{"metric": "unit_purchase_cost"} -> describe_metric{"metric": "labor_cost"} -> describe_metric{"metric": "purchase_amount"} -> describe_metric{"metric": "labor_cost_per_head"}
  - seed 123: list_metrics -> describe_metric{"metric": "labor_cost"} -> describe_metric{"metric": "CAC"} -> describe_metric{"metric": "unit_purchase_cost"} -> describe_metric{"metric": "labor_cost_per_head"}
- `phase7-058`
  - seed 7: list_metrics
  - seed 42: (no tool call)
  - seed 123: query_metric{"end": "2025-12-01", "grain": "total", "metric": "labor_cost", "start": "2025-12-01"} -> list_metrics
- `phase7-059`
  - seed 7: list_metrics -> query_metric{"end": "2025-12-01", "metric": "CAC", "start": "2025-12-01"}
  - seed 42: list_metrics
  - seed 123: list_metrics
- `phase7-063`
  - seed 7: (no tool call)
  - seed 42: trend{"end": "2025-12-01", "grain": "quarter", "metric": "sales_amount", "start": "2025-01-01"}
  - seed 123: (no tool call)
- `phase7-064`
  - seed 7: compare_periods{"kind": "mom", "metric": "sales_amount", "period": "2025-12-01"} -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu...
  - seed 42: compare_periods{"kind": "mom", "metric": "sales_amount", "period": "2025-12-01"} -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu... -> top_n{"ascending": true, "dimension": "product", "end": "2025-12-01", "metric": "sales_amount", "n": 3, "st...
  - seed 123: compare_periods{"kind": "mom", "metric": "sales_amount", "period": "2025-12-01"} -> contribution{"baseline_end": "2025-11-01", "baseline_start": "2025-11-01", "current_end": "2025-12-01", "cu... -> top_n{"ascending": true, "dimension": "product", "end": "2025-12-01", "filters": [{"dimension": "period", "...

Source files:
- seed 7: `data/exports/phase9_2_1_seed_7.json`
- seed 42: `data/exports/phase9_2_1_seed_42.json`
- seed 123: `data/exports/phase9_2_1_seed_123.json`
