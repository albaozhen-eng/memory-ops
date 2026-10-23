#!/usr/bin/env python3
"""W2：golden set 基线跑分器
只读：调用 memory_recall.py 全层检索，判定「应命中证据」是否出现在结果里。
指标：hit@layer-top3 / hit@any / p95 延迟。
用法：
  python3 golden_eval.py            # 跑全量，打印汇总 + 落盘报告
  python3 golden_eval.py --limit 5  # 快速自检
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent          # 本仓库的 scripts/
REPO = HERE.parent                              # 仓库根
CORPUS_DIR = None
# 默认指向**本仓库自带**的 memory_recall.py，这样 clone 后 demo 无需任何部署即可跑。
# 接入自己的系统时用环境变量覆盖，例如：
#   export MEMCHECK_SCRIPTS="$HOME/.openclaw/workspace/scripts"
SCRIPTS = Path(os.environ.get("MEMCHECK_SCRIPTS", HERE))
GOLDEN = Path(os.environ.get("MEM_GOLDEN", HOME / ".openclaw/workspace-main/memory/golden/golden-v0.json"))
REPORT_DIR = Path(os.environ.get("MEM_GOLDEN_REPORTS", REPO / "reports"))
LAYER_ORDER = ["skills", "semantic", "l0", "l1", "facts"]
TOP_PER_LAYER = 3


def recall(query: str, mode: str) -> tuple[dict, int]:
    t0 = time.time()
    env = dict(os.environ)
    if CORPUS_DIR:
        env["MEM_DAILY_NOTES_DIR"] = str(CORPUS_DIR)
    p = subprocess.run(
        ["python3", str(SCRIPTS / "memory_recall.py"), "all", query, "--json"],
        capture_output=True, text=True, timeout=180, cwd=str(SCRIPTS), env=env,
    )
    ms = int((time.time() - t0) * 1000)
    if p.returncode != 0 or not p.stdout.strip():
        return {"layers": {}, "error": (p.stderr or "")[-300:]}, ms
    try:
        return json.loads(p.stdout), ms
    except json.JSONDecodeError:
        return {"layers": {}, "error": "bad json"}, ms


def flatten(data: dict) -> tuple[list[str], list[str]]:
    """返回 (top 池, 全量池) 的文本列表；池 = 各层前 N 条拼接 + 全量。"""
    layers = data.get("layers") or {}
    top, allt = [], []
    for lname in LAYER_ORDER:
        for i, r in enumerate(layers.get(lname) or []):
            txt = r.get("content") or ""
            if lname in ("semantic", "facts"):
                txt += " " + str(r.get("file") or "")
            allt.append(txt)
            if i < TOP_PER_LAYER:
                top.append(txt)
    return top, allt


def hit(tokens: list[str], pool: list[str]) -> bool:
    if not tokens:
        return False
    blob = "\n".join(pool).lower()
    return all(t.lower() in blob for t in tokens)


QUERY_CACHE = GOLDEN.parent / "queries-llm.json"


def load_llm_queries(items) -> dict:
    """用 deepseek-flash 把整句改写成检索关键词（真实可部署的改写器，非人工泄漏）。"""
    if QUERY_CACHE.exists():
        cached = json.loads(QUERY_CACHE.read_text())
        if all(it[0] in cached for it in items):
            return cached

    import urllib.request
    cfg = json.loads((HOME / ".pi/agent/models.json").read_text())["providers"]["deepseek"]
    key, base, model = cfg["apiKey"], cfg["baseUrl"].rstrip("/"), cfg["models"][0]["id"]
    out = {}
    for it in items:
        prompt = ("把下面这个问题改写成 2-4 个用于本地日志检索的中文关键词。"
                  "只输出关键词，空格分隔，不要解释、不要标点。\n\n问题：" + it[2])
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                           "max_tokens": 800, "temperature": 0}).encode()
        req = urllib.request.Request(f"{base}/chat/completions", data=body,
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = json.loads(r.read())["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            txt = it[5]
            print("rewrite failed", it[0], e)
        out[it[0]] = " ".join(txt.split())[:80]
        print(f"  rewrite {it[0]}: {out[it[0]]}")
    QUERY_CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true", help="门禁模式：与已接受基线比较，写 health/golden.json")
    ap.add_argument("--accept", action="store_true", help="把本次结果登记为新的已接受基线")
    ap.add_argument("--golden", type=str, default=str(GOLDEN), help="黄金集 JSON 路径")
    ap.add_argument("--corpus", type=str, default="", help="覆盖 L1 语料目录（demo 用）")
    ap.add_argument("--out", type=str, default="", help="报告输出目录")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--variant", choices=["question", "keywords", "llm", "both"], default="both")
    args = ap.parse_args()

    global CORPUS_DIR
    CORPUS_DIR = Path(args.corpus).resolve() if args.corpus else None
    golden_path = Path(args.golden)
    items = json.loads(golden_path.read_text())
    if args.limit:
        items = items[: args.limit]
    variants = ["question", "keywords"] if args.variant == "both" else [args.variant]
    llm_queries = load_llm_queries(items) if "llm" in variants else {}

    results = []
    for v in variants:
        for it in items:
            q = {"question": it[2], "keywords": it[5]}.get(v) or llm_queries[it[0]]
            data, ms = recall(q, v)
            top, allt = flatten(data)
            results.append({
                "id": it[0], "type": it[1], "variant": v, "query": q,
                "anchors": it[3], "tokens": it[4],
                "hit_top3": hit(it[4], top), "hit_any": hit(it[4], allt),
                "n_results": len(allt), "ms": ms,
                "error": data.get("error"),
                "top_sample": [t[:120] for t in top[:3]],
            })
            mark = "HIT " if hit(it[4], top) else ("weak" if hit(it[4], allt) else "MISS")
            print(f"[{v:8}] {it[0]} {mark:4} {ms:5}ms  {it[2][:38]}")

    # 汇总（排除 R 类：拒答题不适用 recall）
    summary = {}
    for v in variants:
        rs = [r for r in results if r["variant"] == v and r["type"] != "R"]
        if not rs:
            continue
        lat = sorted(r["ms"] for r in rs)
        summary[v] = {
            "n": len(rs),
            "hit_top3": sum(r["hit_top3"] for r in rs),
            "hit_any": sum(r["hit_any"] for r in rs),
            "recall_top3": round(100 * sum(r["hit_top3"] for r in rs) / len(rs), 1),
            "recall_any": round(100 * sum(r["hit_any"] for r in rs) / len(rs), 1),
            "p50_ms": int(statistics.median(lat)),
            "p95_ms": lat[max(0, int(len(lat) * 0.95) - 1)],
            # 检索失败题数。必须单独统计：失败会被记成"未命中"，
            # 于是基线与本次同时为 0 时门禁会报"0 退步"——那是假绿。
            "recall_errors": sum(1 for r in rs if r.get("error")),
        }

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    report_dir = Path(args.out) if args.out else REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    out = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "top_per_layer": TOP_PER_LAYER, "summary": summary, "results": results}
    (report_dir / f"baseline-{stamp}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    _n_refuse = sum(1 for r in results if r["type"] == "R")
    print("\n=== 汇总（不含 {} 道拒答题）===".format(_n_refuse))
    for v, s in summary.items():
        print(f"{v:9} n={s['n']:2}  recall@top3={s['recall_top3']:5}%  "
              f"hit@any={s['recall_any']:5}%  p50={s['p50_ms']}ms  p95={s['p95_ms']}ms")
    report_path = report_dir / f"baseline-{stamp}.json"
    print("报告：", report_path)

    # 默认与题集同目录；demo 用 MEMCHECK_ACCEPTED 指到 .run/，避免污染工作树。
    ACCEPTED = Path(os.environ.get("MEMCHECK_ACCEPTED", golden_path.parent / "baseline-accepted.json"))
    cur = summary.get("question", {}).get("recall_top3")

    if args.accept:
        ACCEPTED.write_text(json.dumps({
            "accepted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "variant": "question", "recall_top3": cur,
            "p95_ms": summary.get("question", {}).get("p95_ms"),
            "report": report_path.name}, ensure_ascii=False, indent=1))
        print("已登记新基线：recall@top3={}%".format(cur))
        return 0

    if args.gate:
        try:
            base = json.loads(ACCEPTED.read_text())
        except (OSError, json.JSONDecodeError):
            base = {"recall_top3": 84.6, "p95_ms": 794, "accepted_at": "seed"}
        # 检索崩溃被记成"未命中" → 与基线同时为 0 → 会报"0 退步、绿"。
        # 分数不可信时必须报红，判词要说清原因。
        cur_err = summary.get("question", {}).get("recall_errors") or 0
        if cur_err:
            delta, level = None, "red"
            verdict = "{} 题检索失败，分数不可信（失败被记成未命中）".format(cur_err)
        elif cur is None or base.get("recall_top3") is None:
            delta, level, verdict = None, "red", "无数据"
        else:
            delta = round(cur - base["recall_top3"], 1)
            if delta > 3:
                level, verdict = "green", "提升 {:+.1f} 点".format(delta)
            elif delta >= -3:
                level, verdict = "green", "持平/正常波动 {:+.1f} 点".format(delta)
            elif delta >= -8:
                level, verdict = "yellow", "退步 {:+.1f} 点".format(delta)
            else:
                level, verdict = "red", "退步 {:+.1f} 点".format(delta)

        HEALTH = Path(os.environ.get("MEMCHECK_HEALTH", REPO / "health"))
        HEALTH.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "overall": level,
            "line": "golden 门禁 {}（当前 {}% / 基线 {}%）".format(verdict, cur, base.get("recall_top3")),
            "current": cur, "baseline": base.get("recall_top3"), "delta": delta,
            "p95_ms": summary.get("question", {}).get("p95_ms"),
            "report": report_path.name,
        }
        (HEALTH / "golden.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        print("")
        print("=== 门禁 ===")
        print(payload["line"], "| p95", payload["p95_ms"], "ms")
        return 2 if level == "red" else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
