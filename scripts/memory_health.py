#!/usr/bin/env python3
from __future__ import annotations

"""记忆系统全局心跳（W1）
单一探针：只读，不改任何现有链路。
输出 ~/.openclaw/health/memory-health.json + 一行人类可读摘要。
退出码：0=全绿/黄，2=有红灯。
"""
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# 默认写到仓库内，避免在别人机器上凭空创建 ~/.openclaw/health。
OUT_DIR = Path(os.environ.get("MEMCHECK_HEALTH", REPO / "health"))
OUT = OUT_DIR / "memory-health.json"

CAPTURE_LOG = HOME / ".openclaw/workspace/memory/memory_capture_30m.log"
MEMORY_MD = Path(os.environ.get("MEM_MEMORY_MD", HOME / ".openclaw/workspace-main/MEMORY.md"))
BUFFER = HOME / ".openclaw/data/memory_buffer.jsonl"
PLUGMEM_ERR = HOME / ".plugmem/logs/service.err.log"
PLUGMEM_DATA = HOME / ".openclaw/plugmem/data"
MEMORY_DIR = Path(os.environ.get("MEM_DAILY_NOTES_DIR", HOME / ".openclaw/workspace-main/memory"))
EVAL_DIR = MEMORY_DIR

NOW = time.time()


def age_h(path: Path) -> float | None:
    try:
        return (NOW - path.stat().st_mtime) / 3600
    except OSError:
        return None


def check(cid, label, age, warn, red, detail="", unit="h"):
    level = "green"
    if age is not None and age >= red:
        level = "red"
    elif age is not None and age >= warn:
        level = "yellow"
    if age is None:
        level, age = "red", None
    return {
        "id": cid,
        "label": label,
        "level": level,
        "value": None if age is None else round(age, 1),
        "unit": unit,
        "warn_at": warn,
        "red_at": red,
        "detail": detail,
    }


def probe_capture():
    a = age_h(CAPTURE_LOG)
    return check("capture", "捕获心跳（30min job）", a, 1.5, 6, "memory_capture_30m.log 的 mtime")


def probe_l1():
    """L1 是事实 SoT：今天或昨天的笔记必须存在且在更新。"""
    today = datetime.now().astimezone()
    cands = [MEMORY_DIR / f"{today:%Y-%m-%d}.md",
             MEMORY_DIR / f"{(today - timedelta(days=1)):%Y-%m-%d}.md"]
    for p in cands:
        if p.exists():
            return check("l1", "L1 每日笔记（SoT）", age_h(p), 26, 50, p.name, unit="h")
    return check("l1", "L1 每日笔记（SoT）", None, 26, 50, "今天和昨天都没有笔记")


def probe_auto_retired():
    """AUTO 区块已于 2026-10-19 退役；若有人把它写回来，这里会变黄。"""
    try:
        text = MEMORY_MD.read_text(errors="ignore")
    except OSError as e:
        return check("auto", "AUTO 退役状态", None, 1, 2, f"读不到：{e}")
    has_stamp = bool(re.search(r"自动同步：\d{4}-\d{2}-\d{2}T", text))
    level = "yellow" if has_stamp else "green"
    return {"id": "auto", "label": "AUTO 退役状态", "level": level,
            "value": 1 if has_stamp else 0, "unit": "处",
            "warn_at": 1, "red_at": None,
            "detail": "检测到自动同步戳，AUTO 可能被写回" if has_stamp else "已退役，仅保留标记位"}


def probe_buffer():
    try:
        n = sum(1 for _ in BUFFER.open())
    except OSError:
        n = 0
    return check("buffer", "写入缓冲区积压", n, 6, 30, "memory_buffer.jsonl 行数", unit="条")


def probe_consolidation():
    """err log 无时间戳 → 用「自上次心跳以来的新增失败数」做增量探针。
    状态存在 ~/.openclaw/health/.consolidation-count，避免重扫全量日志。"""
    state_file = OUT_DIR / ".consolidation-count"
    try:
        total = 0
        with PLUGMEM_ERR.open(errors="ignore") as f:
            for line in f:
                if "failed, skipped" in line:
                    total += 1
    except OSError:
        return check("consolidation", "PlugMem 巩固（新增跳过）", None, 0, 1, "读不到 err log")

    try:
        prev = int(state_file.read_text().strip())
    except (OSError, ValueError):
        prev = total  # 首次运行只记基线，不报红
    delta = total - prev if total >= prev else 0  # 日志轮转则归零
    state_file.write_text(str(total))

    level = "green" if delta == 0 else ("red" if delta > 20 else "yellow")
    return {
        "id": "consolidation",
        "label": "PlugMem 巩固（新增 merge 跳过）",
        "level": level,
        "value": delta,
        "unit": "次/15min",
        "warn_at": 1,
        "red_at": 20,
        "detail": f"累计 {total} 次",
    }


def probe_eval():
    """评测新鲜度：优先看 weekly golden 门禁（health/golden.json），退回旧 karpathy-eval 产物。"""
    gj = HOME / ".openclaw/health/golden.json"
    try:
        d = json.loads(gj.read_text())
        ts = datetime.fromisoformat(d["generated_at"]).astimezone(timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        return check("eval", "golden 门禁最近运行", age, 192, 336,
                     f"{d.get('current')}% vs 基线 {d.get('baseline')}%", unit="h")
    except Exception:
        pass
    files = [p for p in EVAL_DIR.glob("memory-karpathy-eval-*.md")] if EVAL_DIR.exists() else []
    if not files:
        return check("eval", "评测最近运行", None, 192, 336, "无任何评测产物")
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return check("eval", "评测最近运行", age_h(latest), 192, 336, latest.name, unit="h")


def probe_backup():
    """L2 语义图快照：~/.openclaw/backups/plugmem/<YYYY-MM-DD>/，优先读 snapshot.json"""
    root = HOME / ".openclaw/backups/plugmem"
    snaps = [d for d in root.iterdir() if d.is_dir()] if root.exists() else []
    if not snaps:
        return check("backup", "L2 语义图快照", None, 48, 96, f"{root} 下无快照")

    def snap_time(d: Path):
        marker = d / "snapshot.json"
        try:
            return datetime.fromisoformat(json.loads(marker.read_text())["created_at"])
        except Exception:
            try:
                return datetime.strptime(d.name, "%Y-%m-%d").astimezone()
            except ValueError:
                return datetime.fromtimestamp(d.stat().st_mtime).astimezone()

    latest = max(snaps, key=snap_time)
    ts = snap_time(latest).astimezone(timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return check("backup", "L2 语义图快照", age, 48, 96,
                 f"最新 {latest.name}（共 {len(snaps)} 份）", unit="h")


def probe_plugmem():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8089/api/v1/health", timeout=5) as r:
            d = json.loads(r.read())
        ok = all(d.get(k) for k in ("llm_available", "embedding_available", "chroma_available"))
        return {"id": "plugmem", "label": "PlugMem 服务", "level": "green" if ok else "red",
                "value": None, "unit": "", "warn_at": None, "red_at": None,
                "detail": json.dumps(d, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        return {"id": "plugmem", "label": "PlugMem 服务", "level": "red",
                "value": None, "unit": "", "warn_at": None, "red_at": None, "detail": str(e)}


def main():
    checks = [probe_plugmem(), probe_capture(), probe_l1(), probe_auto_retired(), probe_buffer(),
              probe_consolidation(), probe_eval(), probe_backup()]
    reds = [c for c in checks if c["level"] == "red"]
    yellows = [c for c in checks if c["level"] == "yellow"]
    overall = "red" if reds else ("yellow" if yellows else "green")

    def fmt(c):
        v = "?" if c["value"] is None else f"{c['value']}{c['unit']}"
        return f"{c['label']}={v}"

    problems = reds or yellows
    line = ("记忆系统 " + overall.upper() + "：" +
            "；".join(fmt(c) for c in problems)) if problems else "记忆系统全部正常"

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "overall": overall,
        "line": line,
        "checks": checks,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, OUT)
    print(line)
    return 2 if reds else 0


if __name__ == "__main__":
    raise SystemExit(main())
