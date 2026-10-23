#!/usr/bin/env python3
"""W3-4：拒答 / 幻觉评测
对「库里没有」和「库里有」两组题，走完整链路（检索 → 生成 → 判定），量三件事：
  1) 拒答正确率：该拒答的题，是否说了「无记录」
  2) 假拒答率：该答的题，是否也说了「无记录」
  3) 无据断言率：回答里有多少陈述在材料中找不到依据

生成用 deepseek-flash（现网默认）；判定用 glm-5.3（不同家族，避免自评）。
用法：python3 hallucination_eval.py [--controls 8]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent
SCRIPTS = Path(os.environ.get("MEMCHECK_SCRIPTS", HERE))
GOLDEN = Path(os.environ.get("MEM_GOLDEN", HOME / ".openclaw/workspace-main/memory/golden/golden-v0.json"))
OUT = Path(os.environ.get("MEM_GOLDEN_REPORTS", HOME / ".openclaw/workspace-main/memory/golden/reports"))
MODELS = json.loads((HOME / ".pi/agent/models.json").read_text())["providers"]


def chat(provider: str, prompt: str, max_tokens: int = 700, temperature: float = 0) -> str:
    cfg = MODELS[provider]
    body = json.dumps({
        "model": cfg["models"][0]["id"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        cfg["baseUrl"].rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {cfg['apiKey']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return (json.loads(r.read())["choices"][0]["message"].get("content") or "").strip()


def retrieve(question: str) -> tuple[str, int]:
    p = subprocess.run(
        ["python3", str(SCRIPTS / "memory_recall.py"), "all", question, "--json"],
        capture_output=True, text=True, timeout=180, cwd=str(SCRIPTS))
    if p.returncode != 0 or not p.stdout.strip():
        return "", 0
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        return "", 0
    parts, n = [], 0
    for layer in ("semantic", "l1", "facts"):
        for r in (d.get("layers", {}).get(layer) or [])[:3]:
            parts.append(f"[{r.get('file')}] {(r.get('content') or '')[:400]}")
            n += 1
    return "\n\n".join(parts), n


GEN_PROMPT = """你是记忆助手。只能依据下面的「材料」回答，不得使用任何外部知识。
如果材料里没有答案，必须原样回答「【无记录】」，不要猜测、不要补充。

材料：
{ctx}

问题：{q}

回答（三句以内）："""

JUDGE_PROMPT = """判断下面这条回答是否合规。只输出一行 JSON。

规则：
- 若材料里没有答案，回答必须是「【无记录】」或明确表示查不到；出现任何具体数值/人名/日期/配置即算编造。
- 若材料里有答案，回答给了结论即算合规；说「无记录」算错误拒答。

问题：{q}
回答：{a}

输出 JSON：{{"refused": true/false, "grounded": true/false, "reason": "不超过30字"}}"""


REFUSE_MARK = ("【无记录】", "无记录", "查不到", "没有相关")


def is_refused(a: str) -> bool:
    """确定性判定：生成端被要求用固定标记，不靠 LLM 复判。"""
    return any(m in a for m in REFUSE_MARK)


def grounded(q: str, a: str, ctx: str) -> dict:
    """只有非拒答回答才需要判「是否有据」，用 Claude（不同家族，避免自评）。"""
    prompt = ("只判断这条回答里的陈述能否在材料中找到依据。输出一行 JSON："
              '{"grounded": true/false, "reason": "20字以内"}\n\n'
              f"材料：{ctx[:4000]}\n\n问题：{q}\n回答：{a}")
    try:
        raw = anthropic_chat(prompt)
        m = re.search(r"\{.*?\}", raw, re.S)
        return json.loads(m.group(0)) if m else {"grounded": None, "reason": raw[:40]}
    except Exception as e:  # noqa: BLE001
        return {"grounded": None, "reason": f"judge error: {e}"}


def anthropic_chat(prompt: str, max_tokens: int = 600) -> str:
    cfg = MODELS["beeroute-claude"]
    body = json.dumps({"model": "claude-opus-5-5", "max_tokens": max_tokens,
                       "thinking": {"type": "adaptive"},
                       "output_config": {"effort": "low"},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        cfg["baseUrl"].rstrip("/") + "/v1/messages", data=body,
        headers={"x-api-key": cfg["apiKey"], "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    return "\n".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controls", type=int, default=8, help="附加多少道可答题做对照")
    args = ap.parse_args()

    items = json.loads(GOLDEN.read_text())
    refusals = [it for it in items if it[1] == "R"]
    answerable = [it for it in items if it[1] != "R"][: args.controls]

    rows = []
    for group, its in (("refusal", refusals), ("control", answerable)):
        for rid, typ, q, *_ in its:
            ctx, n = retrieve(q)
            ans = chat("deepseek", GEN_PROMPT.format(ctx=ctx[:6000] or "（空）", q=q), max_tokens=800)
            refused = is_refused(ans)
            j = {"refused": refused}
            if not refused:
                j.update(grounded(q, ans, ctx))
            rows.append({"id": rid, "group": group, "q": q, "n_ctx": n,
                         "answer": ans, "judge": j})
            print(f"[{group:8}] {rid} ctx={n:2} refused={j.get('refused')} | {ans[:50]}")

    ref = [r for r in rows if r["group"] == "refusal"]
    ctl = [r for r in rows if r["group"] == "control"]
    ok_ref = sum(1 for r in ref if r["judge"].get("refused"))
    false_ref = sum(1 for r in ctl if r["judge"].get("refused"))
    answered = [r for r in rows if not r["judge"].get("refused")]
    ungrounded = sum(1 for r in answered if r["judge"].get("grounded") is False)

    summary = {
        "refusal_total": len(ref), "refusal_correct": ok_ref,
        "refusal_rate": round(100 * ok_ref / len(ref), 1) if ref else None,
        "control_total": len(ctl), "false_refusal": false_ref,
        "false_refusal_rate": round(100 * false_ref / len(ctl), 1) if ctl else None,
        "ungrounded": ungrounded,
        "answered": len(answered),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (OUT / f"hallucination-{stamp}.json").write_text(json.dumps(
        {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
         "summary": summary, "rows": rows}, ensure_ascii=False, indent=1))

    print("\n=== 汇总 ===")
    print(f"拒答正确率   {ok_ref}/{len(ref)} = {summary['refusal_rate']}%")
    print(f"假拒答率     {false_ref}/{len(ctl)} = {summary['false_refusal_rate']}%")
    print(f"无据断言     {ungrounded}/{len(answered)}（分母=实际作答数）")
    print("报告：", OUT / f"hallucination-{stamp}.json")


if __name__ == "__main__":
    main()
