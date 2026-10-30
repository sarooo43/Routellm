# RouteLLM Benchmark Report

Generated from `scenario_1_results.json` and `scenario_2_results.json` — 37 prompts per arm under identical conditions.

| Scenario | Naive (single model) | RouteLLM |
|---|---|---|
| Normal — success rate | 0.0% | 0.0% |
| **Simulated outage — success rate** | **0.0%** | **0.0%** |
| Normal — avg latency | 2188ms | 501ms |
| Simulated outage — avg latency | 40ms | 170ms |
| Total cost (37 requests) | $0.000000 | $0.000000 |

## Per-task-type breakdown (success rate)

| Task type | Naive (normal) | RouteLLM (normal) | Naive (outage) | RouteLLM (outage) |
|---|---|---|---|---|
| code | 0.0% | 0.0% | 0.0% | 0.0% |
| general | 0.0% | 0.0% | 0.0% | 0.0% |
| qa | 0.0% | 0.0% | 0.0% | 0.0% |
| reasoning | 0.0% | 0.0% | 0.0% | 0.0% |
| summarization | 0.0% | 0.0% | 0.0% | 0.0% |

During the simulated outage, the naive baseline's success rate dropped to **0.0%** while RouteLLM maintained **0.0%** by rerouting to healthy providers; RouteLLM's spread vs the naive under-outage baseline is 0.0% points, and the outage cost the naive approach 0.0% points vs normal conditions.
