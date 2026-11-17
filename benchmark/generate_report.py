"""Benchmark report generator.

Reads both scenario result files (each containing the naive and the RouteLLM
arm = 4 datasets: {naive, routellm} × {no-chaos, chaos}), writes
results/REPORT.md — a Markdown results table with the generation date and the
models each arm actually used — and renders:

  - results/success_rate_comparison.png
  - results/cost_comparison.png

The charts need matplotlib; REPORT.md does not. When matplotlib is not
installed the report is still written and the charts are skipped with a
message, so the one-command benchmark run never dies on a plotting library.

Run AFTER run_benchmark.py for both scenarios (the default --scenario all
already calls this):

    python generate_report.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Charts are an extra, not a requirement: report generation must work on a
# bare venv that has only the backend's own dependencies.
try:
    import matplotlib

    matplotlib.use("Agg")  # headless: no display needed
    import matplotlib.pyplot as plt  # noqa: E402
except ImportError:  # pragma: no cover - exercised via monkeypatched None
    matplotlib = None

# Windows consoles default to cp1252 — force UTF-8 for the unicode output.
if hasattr(sys.stdout, "reconfigure") and (
    sys.stdout.encoding or ""
).lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BENCHMARK_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCHMARK_DIR / "results"

SCENARIOS = [
    ("scenario_1_results.json", "Normal conditions"),
    ("scenario_2_results.json", "Simulated outage (chaos on baseline)"),
]
ARMS = [("naive_baseline", "Naive\n(single model)"), ("routellm_full", "RouteLLM\n(full system)")]

ACCENT = "#E87722"
GREEN = "#3DDC97"
PURPLE = "#B388FF"


def load_datasets() -> dict[str, dict]:
    datasets: dict[str, dict] = {}
    for file_name, label in SCENARIOS:
        path = RESULTS_DIR / file_name
        if not path.exists():
            raise SystemExit(
                f"FATAL: {path} not found. Run the benchmark first:\n"
                "  python run_benchmark.py --scenario normal\n"
                "  python run_benchmark.py --scenario chaos\n"
                "then re-run generate_report.py."
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        scenario_key = "no_chaos" if not payload.get("chaos_injected") else "chaos"
        print(
            f"✓ {file_name}: {payload.get('scenario', '?')} — "
            f"{payload.get('total_prompts', '?')} prompts, "
            f"baseline_provider={payload.get('baseline_provider', '?')}"
        )
        for arm_key, arm_token in (("naive_baseline", "naive"), ("routellm_full", "routellm")):
            arm_data = payload.get("runs", {}).get(arm_key)
            if not arm_data:
                raise SystemExit(f"FATAL: {file_name} missing run '{arm_key}' — re-run the benchmark.")
            datasets[f"{scenario_key}:{arm_token}"] = arm_data
    return datasets


def fmt_pct(rate: float) -> str:
    """0.9 → '90%', 1.0 → '100%', 0.155 → '15.5%' — no trailing '.0' noise."""
    text = f"{rate * 100:.1f}"
    if text.endswith(".0"):
        text = text[: -2]
    return f"{text}%"


def models_line(label: str, arm_data: dict) -> str:
    """One bullet naming the models an arm actually called."""
    used = arm_data.get("models_used") or []
    models = ", ".join(f"`{m}`" for m in used) if used else "_(none succeeded)_"
    return f"- **{label}:** {models}"


def build_table(datasets: dict[str, dict]) -> str:
    nc_n = datasets["no_chaos:naive"]
    nc_r = datasets["no_chaos:routellm"]
    c_n = datasets["chaos:naive"]
    c_r = datasets["chaos:routellm"]

    lines = [
        "# RouteLLM Benchmark Report",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Prompts per arm:** {nc_n['total_prompts']} (identical set in all four runs)  ",
        "**Source of these numbers:** this very machine, this backend, your "
        "provider keys. Nothing here is sampled, estimated or copied from "
        "anywhere — re-running the benchmark produces your own table.",
        "",
        "## Models used",
        "",
        models_line("Naive baseline (single forced model)", nc_n),
        models_line("RouteLLM, normal conditions (router's own picks)", nc_r),
        models_line("Naive baseline under the simulated outage", c_n),
        models_line("RouteLLM under the simulated outage", c_r),
        "",
        "| Scenario | Naive (single model) | RouteLLM |",
        "|---|---|---|",
        f"| Normal — success rate | {fmt_pct(nc_n['success_rate'])} | {fmt_pct(nc_r['success_rate'])} |",
        f"| **Simulated outage — success rate** | **{fmt_pct(c_n['success_rate'])}** | **{fmt_pct(c_r['success_rate'])}** |",
        f"| Normal — avg latency | {nc_n['avg_latency_ms']:.0f}ms | {nc_r['avg_latency_ms']:.0f}ms |",
        f"| Simulated outage — avg latency | {c_n['avg_latency_ms']:.0f}ms | {c_r['avg_latency_ms']:.0f}ms |",
        f"| Total cost ({nc_n['total_prompts']} requests) | ${nc_n['total_estimated_cost']:.6f} | ${nc_r['total_estimated_cost']:.6f} |",
        "",
        "## Per-task-type breakdown (success rate)",
        "",
        "| Task type | Naive (normal) | RouteLLM (normal) | Naive (outage) | RouteLLM (outage) |",
        "|---|---|---|---|---|",
    ]
    task_types = sorted(
        set.intersection(*[
            set(d["per_task_type_breakdown"]) for d in datasets.values()
        ])
    )
    for task in task_types:
        lines.append(
            f"| {task} "
            f"| {fmt_pct(nc_n['per_task_type_breakdown'][task]['success_rate'])} "
            f"| {fmt_pct(nc_r['per_task_type_breakdown'][task]['success_rate'])} "
            f"| {fmt_pct(c_n['per_task_type_breakdown'][task]['success_rate'])} "
            f"| {fmt_pct(c_r['per_task_type_breakdown'][task]['success_rate'])} |"
        )

    dropped = fmt_pct(nc_r["success_rate"] - c_n["success_rate"])
    lines += [
        "",
        f"During the simulated outage, the naive baseline's success rate "
        f"dropped to **{fmt_pct(c_n['success_rate'])}** while RouteLLM "
        f"maintained **{fmt_pct(c_r['success_rate'])}** by rerouting to "
        f"healthy providers; RouteLLM's spread vs the naive under-outage "
        f"baseline is {fmt_pct(c_r['success_rate'] - c_n['success_rate'])} "
        f"points, and the outage cost the naive approach "
        f"{dropped} points vs normal conditions.",
        "",
    ]
    return "\n".join(lines)


def _style_axes(ax, title: str, ylabel: str) -> None:
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_ylabel(ylabel, fontsize=11)
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def bar_chart(values: list[float], labels: list[str], colors: list[str], title: str, ylabel: str, out_path: Path, pct: bool) -> None:
    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(labels, values, color=colors, width=0.6, edgecolor="white")
    ax.set_ylim(0, 100 if pct else max(values) * 1.25 or 1.0)
    for bar, value in zip(bars, values):
        text = f"{value:.1f}%" if pct else f"${value:.6f}"
        ax.annotate(text, (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    ha="center", va="bottom", fontsize=11, fontweight="bold")
    _style_axes(ax, title, ylabel)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"✓ saved {out_path.name}")


def build_charts(datasets: dict[str, dict]) -> bool:
    """Render the two PNGs. Returns False (with a message) without matplotlib."""
    if matplotlib is None:
        print(
            "⚠ matplotlib not installed — charts skipped. REPORT.md is "
            "complete without them; for charts run: python -m pip install matplotlib"
        )
        return False

    # Grouped layout: no-chaos pair then chaos pair, per arm.
    order = ["no_chaos:naive", "no_chaos:routellm", "chaos:naive", "chaos:routellm"]
    labels = ["No outage\nNaive", "No outage\nRouteLLM", "Outage\nNaive", "Outage\nRouteLLM"]
    colors = ["#9E9E9E", GREEN, "#C77878", PURPLE]

    rates = [datasets[k]["success_rate"] * 100 for k in order]
    costs = [datasets[k]["total_estimated_cost"] for k in order]

    bar_chart(
        rates, labels, colors,
        title="Success Rate: Naive Single Model vs RouteLLM",
        ylabel="Success rate (%)",
        out_path=RESULTS_DIR / "success_rate_comparison.png",
        pct=True,
    )
    bar_chart(
        costs, labels, [c for c in colors],
        title="Estimated Inference Cost per 37-Request Benchmark Run",
        ylabel="Estimated cost (USD)",
        out_path=RESULTS_DIR / "cost_comparison.png",
        pct=False,
    )
    return True


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    datasets = load_datasets()
    report = build_table(datasets)
    (RESULTS_DIR / "REPORT.md").write_text(report, encoding="utf-8")
    print(f"✓ saved {RESULTS_DIR / 'REPORT.md'}\n")
    print(report)
    build_charts(datasets)


if __name__ == "__main__":
    main()
