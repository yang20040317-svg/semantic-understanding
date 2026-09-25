#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semantic-understanding / parity.py

跨语言一致性对拍：验证 viz.py 内嵌 JS 的 feats()/score() 与 su.py 完全等价。

为什么非有不可
--------------
面板上的「检索试算器」是 JS 在浏览器里算分的。如果它和终端 `su.py recall`
算得不一样，那用户看到的就是一个**漂亮但说谎的仪表盘** —— 比没有面板更糟。

硬约束（写进 SKILL.md）：改 su.py 的判据 -> 必须同步改 viz.py 的 JS -> 必须跑本对拍。

对拍口径
--------
比对**全库每条案例 × 每个探针**的分数（不是只比 top5），容差 1e-4。
比 top 名次更严格：名次一致可能是巧合，逐条分数一致才能证明判据等价。

用法
----
  python parity.py                      # 默认探针（含三条历史病灶）
  python parity.py "自定义说法1" "自定义说法2"
  python parity.py --quiet              # 只输出结论
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PANEL = ROOT / "viz" / "index.html"
TOL = 1e-4

# 默认探针：前三条是实测出的病灶样本（功能字撞车 / 跨语言残词 / 短句失明），
# 它们必须保持 0 分或极低分；其余是真阳性，必须保持高分。任何一条回归都要警觉。
DEFAULT_QUERIES = [
    "明天要不要带伞",                               # 病灶 1 功能字撞车 -> 应 0
    "我现在的token越用越多是怎么回事，你帮我看看",   # 病灶 2 跨语言残词 -> 应趋 0
    "改改",                                         # 病灶 3 短句失明 -> 走兜底
    "弄一下",                                       # 真阳性：短指令
    "再想想吧",                                     # 真阳性：换方向重做
    "别推别覆盖",                                   # 真阳性：不可逆操作前确认
    "帮我算一下 137 乘以 29",                        # 真无关：应 0
]

JS_RUNNER = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const api = new Function('window', 'document',
  blocks.join('\n') + '\nreturn {score, DATA};'
)({}, { addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; } });
const queries = JSON.parse(process.argv[3]);
const out = {};
for (const q of queries) {
  out[q] = {};
  for (const r of api.DATA.rows) out[q][r.id] = api.score(r, q);
}
process.stdout.write(JSON.stringify(out));
"""


def find_node() -> str | None:
    p = shutil.which("node")
    if p:
        return p
    base = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if base.exists():
        cands = sorted(base.glob("*/node.exe"), reverse=True)
        if cands:
            return str(cands[0])
        cands = sorted(base.glob("*/bin/node"), reverse=True)
        if cands:
            return str(cands[0])
    return None


def py_scores(queries: list[str]) -> dict:
    sys.path.insert(0, str(ROOT))
    import su  # noqa: PLC0415  延迟导入：避免与 CLI 参数解析耦合

    rows = [r for r in su.read_jsonl(su.storage_path("cases", "cases/cases.jsonl"))
            if r.get("status") == "active"]
    return {q: {r["id"]: su.score(r, q) for r in rows} for q in queries}, len(rows)


def js_scores(queries: list[str], node: str) -> dict:
    runner = ROOT / ".parity_runner.js"
    runner.write_text(JS_RUNNER, encoding="utf-8")
    try:
        r = subprocess.run([node, str(runner), str(PANEL), json.dumps(queries, ensure_ascii=False)],
                           capture_output=True, text=True, encoding="utf-8")
    finally:
        runner.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(f"node 执行失败：\n{(r.stderr or '')[:800]}")
    return json.loads(r.stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parity.py", description="su.py 与面板 JS 的判据对拍")
    ap.add_argument("queries", nargs="*", help="自定义探针；缺省用内置 8 条")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    args = ap.parse_args(argv)
    queries = args.queries or DEFAULT_QUERIES

    if not PANEL.exists():
        print("[parity] 面板尚未生成，先跑：python su.py viz", file=sys.stderr)
        return 2
    node = find_node()
    if not node:
        print("[parity] 找不到 node，无法对拍（面板自身仍可用）。", file=sys.stderr)
        return 2

    py, n_rows = py_scores(queries)
    try:
        js = js_scores(queries, node)
    except Exception as e:
        print(f"[parity] {e}", file=sys.stderr)
        return 2

    bad, total = [], 0
    for q in queries:
        for cid, sv in py.get(q, {}).items():
            total += 1
            jv = js.get(q, {}).get(cid)
            if jv is None:
                bad.append((q, cid, sv, None))
            elif abs(float(sv) - float(jv)) > TOL:
                bad.append((q, cid, sv, jv))

    if not args.quiet:
        print(f"[parity] 案例 {n_rows} 条 × 探针 {len(queries)} 个 = {total} 项比对")
        print("         探针：" + " / ".join(q[:18] for q in queries))
        print()
        if bad:
            for q, cid, sv, jv in bad[:20]:
                print(f"  差异  「{q[:24]}」 {cid}  py={sv}  js={jv}")
            if len(bad) > 20:
                print(f"  ... 另有 {len(bad) - 20} 项")
        else:
            print("  逐条一致：面板分数可在终端完整复现，仪表盘不说谎。")

    if bad:
        print(f"\n[parity] 不一致 {len(bad)}/{total} —— 改判据时忘了同步两边？")
        return 1
    print(f"\n[parity] 通过：{total} 项零差异（容差 {TOL}）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
