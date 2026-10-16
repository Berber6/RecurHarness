#!/usr/bin/env python3
"""Run 画像：从 traj.json + eval.json 提取 分数/时长/token/成本/功能块完成率。

用法:
    uv run methods/run_profile.py methods/glm-atlas-baseline-72 --score 72
    uv run methods/run_profile.py methods/* --scores 72,80
输出: 控制台表格 + 每个 run 目录下的 profile.json + methods/PROFILES.md 汇总表
"""
import argparse
import json
from pathlib import Path

# 单价（$/1M token，所里平台 $ 与人民币 1:1）：输入 / 输出 / 缓存输入
PRICE_IN, PRICE_OUT, PRICE_CACHED = 1.1, 3.83, 0.3  # GLM-5.3 主力；Flash: 0.05/0.2/0.016

# atlas 功能块归类规则：按测试文件名关键词归并（顺序即优先级）
BLOCK_RULES = [
    ("basic/cli", ("test_basic", "test_help", "test_version", "test_completion",
                   "test_argument", "test_flags", "test_more_commands", "test_exact_output",
                   "test_output_streams", "test_error_cases", "test_special_cases",
                   "test_smoke", "test_exit_codes", "test_edge_cases", "test_url_formats",
                   "test_cmdstate", "test_subcommand", "test_license", "test_atlas_cli",
                   "test_invocation", "test_unknown")),
    ("migrate", ("test_migrate",)),
    ("schema", ("test_schema",)),
    ("cmdlog", ("test_cmdlog",)),
    ("datasources", ("test_datasource", "test_aws", "test_gcp")),
    ("config/env", ("test_config", "test_env")),
]


def classify(name: str) -> str:
    for block, keys in BLOCK_RULES:
        if any(k in name for k in keys):
            return block
    return "other"


def load_eval(run_dir: Path) -> dict | None:
    files = list(run_dir.glob("*/*.eval.json"))
    return json.loads(files[0].read_text()) if files else None


def profile(run_dir: Path, score: float | None = None) -> dict:
    # rglob 递归收集所有 traj（recursive run 的 implement 轨迹嵌套 2-3 层）
    trajs = sorted(run_dir.rglob("*.traj.json"))
    if not trajs:
        return {"run": run_dir.name, "error": "no traj.json"}
    usages: list[dict] = []
    ts: list[float] = []
    for tf in trajs:
        t = json.loads(tf.read_text())
        for m in t["messages"]:
            if u := m.get("extra", {}).get("response", {}).get("usage"):
                usages.append(u)
            if m.get("extra", {}).get("timestamp"):
                ts.append(m["extra"]["timestamp"])
    pin = sum(u.get("prompt_tokens", 0) for u in usages)
    pout = sum(u.get("completion_tokens", 0) for u in usages)
    cached = sum((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) for u in usages)
    cost = pin / 1e6 * PRICE_CACHED + (pin - cached) / 1e6 * PRICE_IN + pout / 1e6 * PRICE_OUT

    ev = load_eval(run_dir)
    blocks, total_p, total_n = {}, 0, 0
    if ev:
        agg: dict[str, list[int]] = {}
        for r in ev["test_results"]:
            b = classify(r["name"])
            agg.setdefault(b, [0, 0])
            agg[b][0] += r["status"] == "passed"
            agg[b][1] += 1
        blocks = {k: f"{v[0]}/{v[1]} ({v[0] / v[1]:.0%})" for k, v in sorted(agg.items())}
        total_p = sum(v[0] for v in agg.values())
        total_n = sum(v[1] for v in agg.values())
    return {
        "run": run_dir.name,
        "score": score,
        "api_calls": len(usages),
        "duration_h": round((max(ts) - min(ts)) / 3600, 2) if len(ts) >= 2 else None,
        "input_tokens": pin,
        "cached_pct": round(cached / pin, 3) if pin else None,
        "output_tokens": pout,
        "cost": round(cost, 2),
        "max_context": max((u.get("prompt_tokens", 0) for u in usages), default=0),
        "blocks": blocks,
        "test_pass": f"{total_p}/{total_n} ({total_p / total_n:.0%})" if total_n else None,
    }


def print_report(r: dict) -> None:
    print(f"== {r['run']}")
    for k in ["score", "api_calls", "duration_h", "input_tokens", "cached_pct",
              "output_tokens", "cost", "max_context", "test_pass"]:
        print(f"  {k:<14} {r.get(k)}")
    if r.get("blocks"):
        print(f"  {'功能块':<14} 通过/总数 (完成率)")
        for b, v in r["blocks"].items():
            print(f"  {b:<14} {v}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--score", type=float)
    ap.add_argument("--scores", help="逗号分隔，与 runs 一一对应")
    args = ap.parse_args()
    scores = [float(x) for x in args.scores.split(",")] if args.scores else (
        [args.score] if args.score is not None else [None] * len(args.runs)
    )
    reports = [profile(rd, s) for rd, s in zip(args.runs, scores)]
    for rd, r in zip(args.runs, reports):
        print_report(r)
        (rd / "profile.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
    md = Path(__file__).parent / "PROFILES.md"
    lines = ["# Run 画像汇总", "",
             "| run | score | 时长h | cost | 输入token | 缓存% | 输出token | max_ctx |",
             "|---|---|---|---|---|---|---|---|"]
    for r in reports:
        lines.append(f"| {r['run']} | {r.get('score')} | {r.get('duration_h')} | {r.get('cost')} | "
                     f"{r.get('input_tokens'):,} | {r.get('cached_pct')} | {r.get('output_tokens'):,} | "
                     f"{r.get('max_context'):,} |")
    md.write_text("\n".join(lines) + "\n")
    print(f"\nprofile.json 已写入各 run 目录；汇总见 {md}")


if __name__ == "__main__":
    main()
