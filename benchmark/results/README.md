# Benchmark results

**This folder ships empty — on purpose.**

Every file that belongs here (`scenario_1_results.json`, `scenario_2_results.json`,
`REPORT.md`, the two comparison PNGs) is produced by **your own benchmark run**
on your own machine, against your own backend and provider keys. Nothing is
pre-computed and committed, because a number taken from someone else's run
would say nothing about your setup — and a placeholder number would look
real while meaning nothing.

To fill this folder (backend already running, see `../README.md`):

```bash
# from benchmark/ — one command does both scenarios + the report
backend/.venv/Scripts/python.exe run_benchmark.py        # Windows
.venv/bin/python run_benchmark.py                        # macOS/Linux
```

Outputs that will appear here:

| File | Contents |
|---|---|
| `scenario_1_results.json` | normal conditions — both arms' aggregates + per-prompt rows |
| `scenario_2_results.json` | chaos outage — both arms' aggregates + per-prompt rows |
| `REPORT.md` | Markdown results table: date, models used, naive vs router success rates |
| `success_rate_comparison.png` | 4-bar success-rate chart (needs matplotlib) |
| `cost_comparison.png` | 4-bar cost chart (needs matplotlib) |
