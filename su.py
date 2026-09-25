#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semantic-understanding / su.py

记录「你的原话  ->  你的真意」配对案例，提炼个人表达习惯，供后续对话注入。
设计要点：
  - 零外部依赖，标准库实现
  - 检索判据：分段取特征 + 功能字过滤 + 长度加权（见「文本匹配」段说明）
  - 双通道生效：cases.jsonl 为长尾（按需 recall），profile.md 为常驻（Top N 高置信规则）
  - 所有写入原子化 UTF-8；控制台重编码为 utf-8，兼容 Windows GBK 终端

命令：
  capture  登记一条候选案例（误解 -> 纠正）
  pending  列出待确认候选
  confirm  确认候选 -> 正式入库
  reject   丢弃候选
  cases    列出正式案例
  recall   按当前话题检索相关规则/case，输出可注入的 markdown 块
  promote  把被 >=N 个案例支撑的 rule 升级进常驻档案
  profile  输出常驻档案全文
  stats    统计概览
  forget   退役一条 case（说错了/过时了）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "promote_threshold": 2,
    "max_profile_rules": 20,
    "inject_top_k": 5,
    "min_score": 0.22,       # 判据重写后量纲变了，旧的 0.06 已无意义
    "tail_ratio": 0.6,       # 其余条目须达到 top1 × 此值才允许注入
    "short_prompt_len": 6,   # 汉字段短于此长度且无命中 -> 走短句兜底
    "fallback_case_id": "c-20260925-005",
}

BEGIN_MARK = "<!-- BEGIN CORE RULES -->"
END_MARK = "<!-- END CORE RULES -->"


# ---------------------------------------------------------------- 基础设施

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def storage_path(key: str, fallback: str) -> Path:
    cfg = load_config()
    rel = (cfg.get("storage") or {}).get(key, fallback)
    return ROOT / rel


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_jsonl(path: Path, records: list[dict]) -> None:
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    write_text(path, body)


def next_id(prefix: str, *sources: Path) -> str:
    day = date.today().strftime("%Y%m%d")
    seen = 0
    for src in sources:
        for rec in read_jsonl(src):
            rid = str(rec.get("id", ""))
            if rid.startswith(f"{prefix}-{day}-"):
                try:
                    seen = max(seen, int(rid.rsplit("-", 1)[-1]))
                except Exception:
                    pass
    return f"{prefix}-{day}-{seen + 1:03d}"


# ---------------------------------------------------------------- 文本匹配

CJK = r"\u4e00-\u9fff"
_CJK_RE = re.compile(rf"[{CJK}]")
_LATIN_RE = re.compile(r"[a-z0-9_]")

# 中文功能字。**这是虚字撞车的主凶**：旧判据下「明天要不要带伞」靠 `不要`/`要不`
# 撞上「要不要登记」拿到 0.6433，比真命中还高。任一 n-gram 若全由这些字构成，
# 视为无信息量直接丢弃。
STOP_CHARS = set(
    "的地得了着过是在我你他她它们咱这那哪有和与或及不没无别也就都还又再只才"
    "而但却可请让把被给对从到往向于之其此等以为因所如若则且并即"
    "吗呢吧啊呀么什怎样些个会能要想该应需很太更最说一人二三四五六七八九十"
)

# 长度即可信度：虚字撞车全是 2-gram，真实语义重合通常带 3/4-gram。
_NGRAM_W = {2: 0.45, 3: 0.85, 4: 1.15}
_LATIN_W = 1.30      # 拉丁整词
_LATIN_N = 4         # 拉丁不切 2/3-gram：`token` 的 `to`/`en` 会撞上 `documentary`
_LATIN_NW = 0.60


def segments(text: str) -> list[tuple[str, str]]:
    """切成 (类型, 片段)：c = 汉字段，l = 拉丁字母/数字段。
    分段的意义：读作整体才不会被跨语言残词切出假特征。"""
    out: list[tuple[str, str]] = []
    cur: list[str] = []
    kind = ""
    for ch in (text or "").lower():
        if _CJK_RE.match(ch):
            k = "c"
        elif _LATIN_RE.match(ch):
            k = "l"
        else:
            k = ""
        if not k:
            if cur:
                out.append((kind, "".join(cur)))
                cur, kind = [], ""
            continue
        if k != kind:
            if cur:
                out.append((kind, "".join(cur)))
            cur, kind = [ch], k
        else:
            cur.append(ch)
    if cur:
        out.append((kind, "".join(cur)))
    return out


@lru_cache(maxsize=8192)
def feats(text: str) -> dict[str, float]:
    """提纯特征 {n-gram: 权重}。

    **必须与 viz.py 内嵌 JS 的 feats() 同判准**，否则面板上的分数不可信。
    """
    out: dict[str, float] = {}
    for kind, seg in segments(text):
        if kind == "l":
            if len(seg) >= 2:
                out[seg] = _LATIN_W
            if len(seg) >= _LATIN_N:
                for i in range(len(seg) - _LATIN_N + 1):
                    out.setdefault(seg[i:i + _LATIN_N], _LATIN_NW)
        else:
            for n, w in _NGRAM_W.items():
                if len(seg) < n:
                    continue
                for i in range(len(seg) - n + 1):
                    g = seg[i:i + n]
                    if g in out:
                        continue
                    if all(c in STOP_CHARS for c in g):
                        continue
                    out[g] = w
    return out


_FIELD_WEIGHTS = {
    "rule": 1.0,
    "tags": 0.95,
    "domain": 0.8,
    "surface": 0.7,
    "intended": 0.6,
    "literal": 0.25,
}


def field_text(rec: dict, field: str) -> str:
    v = rec.get(field)
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v)


def haystack(rec: dict) -> str:
    """所有字段拼成的检索面，用于字面短语命中判断。"""
    return " ".join(field_text(rec, f) for f in
                    ("surface", "intended", "rule", "domain", "literal", "tags", "correction"))


def score(rec: dict, query: str) -> float:
    """加权覆盖率取最大值 + 长片段字面命中加成 + 证据数微加权。

    与旧版的差别：
      - 覆盖率按**特征权重**算（长 n-gram 权重高），不再按命中个数
      - **零重合直接返回 0**：旧版会无条件白送 `hits * 0.01`，让 `改改`
        这种本该 0 分的拿到 0.01，把漏命中伪装成「有命中」
    """
    q = feats(query)
    if not q:
        return 0.0
    qw = sum(q.values())
    best = 0.0
    for field, fw in _FIELD_WEIGHTS.items():
        g = feats(field_text(rec, field))
        if not g:
            continue
        inter = sum(w for gram, w in q.items() if gram in g)
        if inter:
            best = max(best, (inter / qw) * fw)
    if best <= 0.0:
        return 0.0
    hay = haystack(rec)
    bonus = 0.0
    for gram in q:
        if len(gram) >= 3 and gram in hay:
            bonus += min(len(gram) - 1, 4) * 0.045
    best += min(bonus, 0.45)
    best += min(int(rec.get("hits", 1) or 1), 5) * 0.01
    return round(min(best, 0.99), 4)


def select_hits(rows: list[dict], query: str, cfg: dict | None = None
                ) -> tuple[list[tuple[float, dict]], bool]:
    """决定「哪些条目值得注入」。返回 (命中列表, 是否走了短句兜底)。

    注入门控三道（2026-09-25 加，治的是「一群噪声撞车都挤进来」）：
      1. 硬门槛 min_score
      2. **领先判据** —— 其余条目须达到 top1 × tail_ratio，滤掉跟在真命中
         后面的弱相关噪音（旧版只有硬门槛，噪声和真命中同权注入）
      3. **短句兜底** —— 极短指令靠字面永远判不出语义（`改改` 得 0.00），
         命不中任何规则时按结构特征兜到 fallback_case
    """
    cfg = cfg or load_config()
    min_score = float(cfg.get("min_score", 0.22))
    tail_ratio = float(cfg.get("tail_ratio", 0.6))
    scored = []
    for r in rows:
        s = score(r, query)
        if s >= min_score:
            scored.append((s, r))
    scored.sort(key=lambda x: (-x[0], -int(x[1].get("hits", 1))))
    if scored:
        floor = max(min_score, scored[0][0] * tail_ratio)
        return [x for x in scored if x[0] >= floor], False
    # 短句兜底：话越短，字面判据越失明
    fb_id = cfg.get("fallback_case_id")
    if fb_id and len(_CJK_RE.findall(query or "")) <= int(cfg.get("short_prompt_len", 6)):
        for r in rows:
            if r.get("id") == fb_id:
                return [(min_score, r)], True
    return [], False


# ---------------------------------------------------------------- 命令实现

def cmd_capture(args) -> int:
    pending_p = storage_path("pending", "pending/candidates.jsonl")
    cases_p = storage_path("cases", "cases/cases.jsonl")
    rec = {
        "id": next_id("c", pending_p, cases_p),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "surface": args.surface.strip(),
        "literal": (args.literal or "").strip(),
        "intended": args.intended.strip(),
        "correction": (args.correction or "").strip(),
        "rule": (args.rule or "").strip(),
        "domain": (args.domain or "general").strip(),
        "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
        "hits": 1,
        "status": "pending",
        "source": args.source,
    }
    if not rec["surface"] or not rec["intended"]:
        print("[su] capture 至少需要 --surface 与 --intended", file=sys.stderr)
        return 2
    if not rec["rule"]:
        rec["rule"] = f"当他说「{rec['surface']}」，实指：{rec['intended']}"
    append_jsonl(pending_p, rec)
    print(f"[su] 已登记候选 {rec['id']}")
    print(f"     原话  ：{rec['surface']}")
    print(f"     真意  ：{rec['intended']}")
    print(f"     规则  ：{rec['rule']}")
    print("     待 confirm 后才会进入检索库。")
    return 0


def cmd_pending(args) -> int:
    pending_p = storage_path("pending", "pending/candidates.jsonl")
    rows = [r for r in read_jsonl(pending_p) if r.get("status") == "pending"]
    if not rows:
        print("[su] 暂无待确认候选。")
        return 0
    print(f"[su] 待确认候选 {len(rows)} 条：\n")
    for i, r in enumerate(rows, 1):
        print(f"  {i}. {r['id']}  [{r.get('domain','general')}]")
        print(f"     原话：{r.get('surface','')}")
        print(f"     真意：{r.get('intended','')}")
        if r.get("rule"):
            print(f"     规则：{r.get('rule','')}")
        print()
    print("确认 → su.py confirm <id>    丢弃 → su.py reject <id>")
    return 0


def _move_out(pending_p: Path, cid: str) -> tuple[dict | None, list[dict]]:
    rows = read_jsonl(pending_p)
    target, rest = None, []
    for r in rows:
        if r.get("id") == cid and target is None:
            target = r
        else:
            rest.append(r)
    return target, rest


def cmd_confirm(args) -> int:
    pending_p = storage_path("pending", "pending/candidates.jsonl")
    cases_p = storage_path("cases", "cases/cases.jsonl")
    target, rest = _move_out(pending_p, args.id)
    if target is None:
        print(f"[su] 未找到待确认候选 {args.id}", file=sys.stderr)
        return 1
    write_jsonl(pending_p, rest)

    # 同 rule 已存在 -> 累加证据而不是新增重复条目
    existing = read_jsonl(cases_p)
    merged = False
    for rec in existing:
        if rec.get("status") == "active" and rec.get("rule") == target.get("rule"):
            rec["hits"] = int(rec.get("hits", 1)) + 1
            ev = rec.get("evidence") or []
            ev.append({"ts": target.get("ts"), "surface": target.get("surface")})
            rec["evidence"] = ev
            merged = True
            target_group_id = rec["id"]
            break
    if not merged:
        target["status"] = "active"
        target["confirmed_ts"] = datetime.now().isoformat(timespec="seconds")
        existing.append(target)
        target_group_id = target["id"]
    write_jsonl(cases_p, existing)

    print(f"[su] 已确认 {args.id} -> 案例库（归属 {target_group_id}，"
          f"{'证据累加' if merged else '新建条目'}）")
    return 0


def cmd_reject(args) -> int:
    pending_p = storage_path("pending", "pending/candidates.jsonl")
    target, rest = _move_out(pending_p, args.id)
    if target is None:
        print(f"[su] 未找到候选 {args.id}", file=sys.stderr)
        return 1
    write_jsonl(pending_p, rest)
    print(f"[su] 已丢弃候选 {args.id}（不入案例库）。")
    return 0


def cmd_cases(args) -> int:
    cases_p = storage_path("cases", "cases/cases.jsonl")
    rows = [r for r in read_jsonl(cases_p) if r.get("status") == "active"]
    if getattr(args, "domain", None):
        rows = [r for r in rows if r.get("domain") == args.domain]
    if not rows:
        print("[su] 案例库为空。")
        return 0
    rows.sort(key=lambda r: int(r.get("hits", 1)), reverse=True)
    print(f"[su] 正式案例 {len(rows)} 条：\n")
    for r in rows:
        print(f"  {r['id']}  x{int(r.get('hits',1))}  [{r.get('domain','general')}]")
        print(f"     原话：{r.get('surface','')}")
        print(f"     真意：{r.get('intended','')}")
        print(f"     规则：{r.get('rule','')}")
        print()
    return 0


def cmd_recall(args) -> int:
    cfg = load_config()
    cases_p = storage_path("cases", "cases/cases.jsonl")
    rows = [r for r in read_jsonl(cases_p) if r.get("status") == "active"]
    hits, fell_back = select_hits(rows, args.intent, cfg)

    core = core_rules()
    core_ids = {cid for _, _, _, cid in core}
    prof_name = storage_path("profile", "profile/expression-profile.md").name

    top = hits[: int(cfg.get("inject_top_k", 5))]
    longtail = [(s, r) for s, r in top if r.get("id") not in core_ids]

    if not longtail:
        # 必须区分两种「空」：真没命中（要走 L1 协议复述）vs 命中的都已常驻
        # （无需复述，会话开始就注入过了）。混为一谈会让 recall 的输出骗人。
        if top:
            print(f"[su] 无新的长尾命中：{len(top)} 条命中均已收录在常驻档案，不重复输出。")
        else:
            print("[su] 无命中。按 Level 1 协议：执行前先用一句话复述你的理解。")
        if core:
            print(f"     常驻规则 {len(core)} 条见 {prof_name}"
                  f"（会话开始已注入，要打包成一块用 --with-core）。")
        return 0

    if fell_back:
        print("## 语义对齐 · 短句兜底\n")
        print("> 这句话太短，字面判据必然失明 —— 按结构特征兜到「短指令」规则：\n")
    else:
        print("## 语义对齐 · 本次长尾命中\n")
    for s, r in longtail:
        print(f"- **[{r.get('domain','general')} · x{int(r.get('hits',1))} · 匹配{s}]** "
              f"他说「{r.get('surface','')}」→ 实指 {r.get('intended','')}")

    if args.with_core:
        if core:
            print("\n**常驻规则（带 --with-core 一并提供）：**\n")
            for text, hits, domain, _cid in core[: int(cfg.get("max_profile_rules", 20))]:
                print(f"- **[常驻 · {domain} · x{hits}]** {text}")
    elif core:
        print(f"\n> 常驻规则 {len(core)} 条见 `{storage_path('profile', 'profile/expression-profile.md').name}`"
              f"（会话开始已注入，此处不重复）。要打包成一块用 `--with-core`。")

    print("\n> 以上优先于字面理解。没有覆盖到的说法，执行前复述确认，别猜。")
    return 0


def cmd_stats(args) -> int:
    cases_p = storage_path("cases", "cases/cases.jsonl")
    pending_p = storage_path("pending", "pending/candidates.jsonl")
    prof_p = storage_path("profile", "profile/expression-profile.md")
    cases = read_jsonl(cases_p)
    active = [r for r in cases if r.get("status") == "active"]
    retired = [r for r in cases if r.get("status") == "retired"]
    pending = [r for r in read_jsonl(pending_p) if r.get("status") == "pending"]
    domains = {}
    for r in active:
        d = r.get("domain", "general")
        domains[d] = domains.get(d, 0) + 1
    print("[su] semantic-understanding 概览")
    print(f"  正式案例   ：{len(active)} 条（累计证据 {sum(int(r.get('hits',1)) for r in active)} 次）")
    print(f"  退役案例   ：{len(retired)} 条")
    print(f"  待确认候选 ：{len(pending)} 条")
    print(f"  常驻规则   ：{len(core_rules())} 条 <- {prof_p.name}")
    if domains:
        print("  领域分布   ：" + "，".join(f"{k}={v}" for k, v in sorted(domains.items(), key=lambda x: -x[1])))
    return 0


def _toggle_pin(args, value: bool) -> int:
    cases_p = storage_path("cases", "cases/cases.jsonl")
    rows = read_jsonl(cases_p)
    hit = False
    for r in rows:
        if r.get("id") == args.id:
            r["pinned"] = value
            hit = True
            break
    if not hit:
        print(f"[su] 未找到案例 {args.id}", file=sys.stderr)
        return 1
    write_jsonl(cases_p, rows)
    verb = "已置顶" if value else "已取消置顶"
    print(f"[su] {verb} {args.id}。"
          f"{'它将无视证据阈值进入常驻档案，跑 promote 生效。' if value else '跑 promote 同步常驻档案。'}")
    return 0


def cmd_pin(args) -> int:
    return _toggle_pin(args, True)


def cmd_unpin(args) -> int:
    return _toggle_pin(args, False)


def cmd_forget(args) -> int:
    cases_p = storage_path("cases", "cases/cases.jsonl")
    rows = read_jsonl(cases_p)
    hit = False
    for r in rows:
        if r.get("id") == args.id:
            r["status"] = "retired"
            r["retired_ts"] = datetime.now().isoformat(timespec="seconds")
            hit = True
            break
    if not hit:
        print(f"[su] 未找到案例 {args.id}", file=sys.stderr)
        return 1
    write_jsonl(cases_p, rows)
    print(f"[su] 已退役 {args.id}。记得跑 promote 同步常驻档案。")
    return 0


# ---------------------------------------------------------------- 常驻档案

def locate_marks(text: str) -> tuple[int, int] | None:
    """定位真正的 MAGIC 区块边界。必须用 rindex：文件头说明文字里也会提到
    标记本身，从左切会切到说明行上，导致第二次 promote 重复追加规则。"""
    b, e = text.rfind(BEGIN_MARK), text.rfind(END_MARK)
    if b < 0 or e < 0 or e < b:
        return None
    return b, e


def core_rules() -> list[tuple[str, int, str, str]]:
    """从 profile.md 的 MAGIC 区块解析 core rules。
    返回 [(rule_text, hits, domain, case_id)]"""
    prof_p = storage_path("profile", "profile/expression-profile.md")
    if not prof_p.exists():
        return []
    text = prof_p.read_text(encoding="utf-8")
    loc = locate_marks(text)
    if loc is None:
        return []
    b, e = loc
    body = text[b + len(BEGIN_MARK):e]
    out = []
    for line in body.splitlines():
        m = re.match(r"^\s*-\s+\[(?P<meta>[^\]]*)\]\s*(?P<rest>.+)$", line)
        if not m:
            continue
        meta, rest = m.group("meta"), m.group("rest").strip()
        hits = 1
        mh = re.search(r"x(\d+)", meta)
        if mh:
            hits = int(mh.group(1))
        cid = ""
        mc = re.search(r"id:([A-Za-z0-9\-]+)", meta)
        if mc:
            cid = mc.group(1)
        out.append((rest, hits, meta.split("·")[0].strip(), cid))
    return out


def cmd_promote(args) -> int:
    cfg = load_config()
    cases_p = storage_path("cases", "cases/cases.jsonl")
    prof_p = storage_path("profile", "profile/expression-profile.md")
    rows = [r for r in read_jsonl(cases_p) if r.get("status") == "active"]

    threshold = int(args.threshold or cfg.get("promote_threshold", 2))
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = (r.get("rule") or "").strip()
        if not key:
            continue
        groups.setdefault(key, []).append(r)

    qualified = [(k, v) for k, v in groups.items()
                 if sum(int(x.get("hits", 1)) for x in v) >= threshold or any(x.get("pinned") for x in v)]
    qualified.sort(key=lambda kv: -sum(int(x.get("hits", 1)) for x in kv[1]))
    qualified = qualified[: int(cfg.get("max_profile_rules", 20))]

    lines = [f"- [{v[0].get('domain','general')} · x{sum(int(x.get('hits',1)) for x in v)} "
             f"· id:{v[0]['id']}] {k}" for k, v in qualified]

    old = prof_p.read_text(encoding="utf-8") if prof_p.exists() else ""
    loc = locate_marks(old) if old else None
    if loc:
        b, e = loc
        head, tail = old[:b], old[e + len(END_MARK):]
        head = re.sub(r"(?m)^rules:\s*\d+", f"rules: {len(qualified)}", head)
        head = re.sub(r"(?m)^updated:\s*\S+", f"updated: {date.today().isoformat()}", head)
        new = head + BEGIN_MARK + "\n" + "\n".join(lines) + "\n" + END_MARK + tail
    else:
        header = (
            "---\n"
            f"updated: {date.today().isoformat()}\n"
            f"rules: {len(qualified)}\n"
            "generator: semantic-understanding/su.py promote\n"
            "---\n\n"
            "# 表达习惯档案 · Core Rules\n\n"
            "> 由 semantic-understanding 自动生成。命中时优先于字面理解。\n"
            f"> 仅收录被 >= {threshold} 次证据（或被 `pin` 置顶）支撑的规则；"
            "单条案例仍在长尾库中，可被 recall 检索。\n"
            f"> `{BEGIN_MARK}` 与 `{END_MARK}` 之间为生成区，勿手改；下方为手动保护区。\n\n"
        )
        body = BEGIN_MARK + "\n" + "\n".join(lines) + "\n" + END_MARK
        tail = "\n\n## 手动补充（保护区，不被覆盖）\n\n- 在这里写你希望长期生效的表达偏好。\n"
        new = header + body + tail

    write_text(prof_p, new)
    print(f"[su] 常驻档案已更新：{len(qualified)} 条（阈值 threshold={threshold}）")
    for k, v in qualified:
        print(f"     x{sum(int(x.get('hits',1)) for x in v)}  {k}")
    if not qualified:
        print("     （尚无规则达到阈值；单条案例仍可通过 recall 命中）")
    return 0


def cmd_profile(args) -> int:
    prof_p = storage_path("profile", "profile/expression-profile.md")
    if not prof_p.exists():
        print("[su] 常驻档案尚未生成，跑 promote 试试。")
        return 0
    print(prof_p.read_text(encoding="utf-8"))
    return 0


# ---------------------------------------------------------------- 可视化

# 任何写操作成功后都会静默重绘 viz/index.html（由 auto_viz 控制）。
# 立库即为可视化：不需要用户记得跑命令，也不需要额外请求。
MUTATING = {"capture", "confirm", "reject", "promote", "pin", "unpin", "forget"}


def load_viz():
    """延迟加载 viz 渲染器。返回 None 表示不可用，调用方须容错。"""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import viz  # noqa: F401
        return viz
    except Exception as e:  # 渲染器缺失不得拖垮数据层
        print(f"[su] 可视化渲染器不可用（数据不受影响）：{e}", file=sys.stderr)
        return None


def auto_viz(changed: str) -> None:
    """写操作后的寂静重绘：失败只提示，不改变主命令返回码。"""
    if not load_config().get("auto_viz", True):
        return
    viz = load_viz()
    if viz is None:
        return
    try:
        out = viz.render(viz.default_out())
        print(f"[su] 面板已同步重绘（{changed}）-> {out}")
    except Exception as e:
        print(f"[su] 面板重绘失败（数据已写入，不影响使用）：{e}", file=sys.stderr)


def cmd_viz(args) -> int:
    viz = load_viz()
    if viz is None:
        return 1
    argv = []
    if args.open:
        argv.append("--open")
    return viz.main(argv)


# ---------------------------------------------------------------- 自检

def cmd_doctor(args) -> int:
    """体检：常驻机制是否还活着。

    防的是**静默失效**——hook 命令里写的是解释器绝对路径，一旦 WorkBuddy 升级
    managed Python、版本目录改名，hook 就无声无息地不再注入。届时表现只是
    「AI 怎么又听不懂了」，没人会想到是配置断了。这个命令把它变成可检查的。
    """
    import os
    import sys as _sys

    ok, warn, bad = [], [], []

    prof_p = storage_path("profile", "profile/expression-profile.md")
    cases_p = storage_path("cases", "cases/cases.jsonl")
    hook_p = ROOT / "hook.py"
    settings_p = Path.home() / ".workbuddy" / "settings.json"

    # 1. 数据层
    if cases_p.exists():
        n = len([r for r in read_jsonl(cases_p) if r.get("status") == "active"])
        ok.append(f"案例库 {n} 条")
    else:
        bad.append("案例库缺失")
    cr = core_rules()
    if cr:
        ok.append(f"常驻规则 {len(cr)} 条（{prof_p.name}）")
    else:
        warn.append("常驻规则为 0 —— 跑 promote，或用 pin 置顶核心规则")

    # 2. hook 脚本存在
    if hook_p.exists():
        ok.append("hook.py 存在")
    else:
        bad.append("hook.py 缺失 —— 常驻注入无法工作")

    # 3. hooks 配置
    cmd = None
    if not settings_p.exists():
        bad.append(f"找不到 {settings_p}")
    else:
        try:
            s = json.loads(settings_p.read_text(encoding="utf-8"))
            entries = ((s.get("hooks") or {}).get("UserPromptSubmit") or [])
            for e in entries:
                for h in (e.get("hooks") or []):
                    if h.get("type") == "command" and "semantic-understanding" in str(h.get("command", "")):
                        cmd = str(h["command"])
            if cmd:
                ok.append("UserPromptSubmit hook 已挂载")
            else:
                bad.append("settings.json 里没有本技能的 UserPromptSubmit hook")
        except Exception as ex:
            bad.append(f"settings.json 读取失败：{ex}")

    # 4. 命令里的路径是否还有效（核心：防版本漂移）
    if cmd:
        paths = re.findall(r'"([^"]+)"', cmd) or cmd.split()
        for pth in paths:
            if pth.endswith(".exe"):
                if os.path.isfile(pth):
                    ok.append(f"解释器有效：{pth}")
                    if os.path.normcase(os.path.abspath(pth)) != os.path.normcase(os.path.abspath(_sys.executable)):
                        warn.append(f"配置里的解释器与当前运行的不同：当前 {_sys.executable}")
                else:
                    bad.append(f"解释器路径已失效：{pth}")
            elif pth.endswith(".py"):
                if os.path.isfile(pth):
                    ok.append(f"脚本有效：{Path(pth).name}")
                else:
                    bad.append(f"脚本路径已失效：{pth}")

    # 5. 自动重绘开关
    if load_config().get("auto_viz", True):
        ok.append("auto_viz 开启（写库后自动重绘面板）")
    else:
        warn.append("auto_viz 关闭 —— 面板需手动跑 viz")
    if (ROOT / "viz" / "index.html").exists():
        ok.append("可视化面板已生成")
    else:
        warn.append("面板尚未生成，跑 viz")

    # 6. 旧 shell 壳脚本若仍在，提示清理
    if (ROOT / "hook.sh").exists():
        warn.append("hook.sh 仍在（已被弃用：本机 bash 命令名解析到 WSL 被安全策略拦截）")

    print("[su] 常驻机制体检\n")
    for x in ok:
        print(f"  正常  {x}")
    for x in warn:
        print(f"  注意  {x}")
    for x in bad:
        print(f"  故障  {x}")
    print()
    if bad:
        print("  结论：常驻机制不可用，按上面「故障」项修复。")
        print(f"  修复解释器路径：改 {settings_p} 里 hooks.UserPromptSubmit 的 command，")
        print(f"  指向当前有效的 python.exe（现在可用：{_sys.executable}）。")
        return 1
    print("  结论：常驻机制正常 —— 用户每发一句话都会自动注入命中的表达习惯。")
    return 0


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="su.py",
        description="semantic-understanding：记录「你的原话 -> 你的真意」，让 AI 越来越懂你的表达。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="登记一条误解->纠正候选")
    c.add_argument("--surface", required=True, help="用户的原话（尽量原文）")
    c.add_argument("--intended", required=True, help="用户真正想要的意思")
    c.add_argument("--literal", default="", help="AI 当时按字面理解成了什么")
    c.add_argument("--correction", default="", help="用户做了什么纠正动作")
    c.add_argument("--rule", default="", help="提炼出的通用规则，缺省自动生成")
    c.add_argument("--domain", default="general", help="领域标签，如 ui-design / 工程 / 写作")
    c.add_argument("--tags", default="", help="逗号分隔关键词")
    c.add_argument("--source", default="auto", choices=["auto", "manual", "seed"])
    c.set_defaults(func=cmd_capture)

    c = sub.add_parser("pending", help="列出待确认候选")
    c.set_defaults(func=cmd_pending)

    c = sub.add_parser("confirm", help="确认候选入库")
    c.add_argument("id")
    c.set_defaults(func=cmd_confirm)

    c = sub.add_parser("reject", help="丢弃候选")
    c.add_argument("id")
    c.set_defaults(func=cmd_reject)

    c = sub.add_parser("cases", help="列出正式案例")
    c.add_argument("--domain", default=None)
    c.set_defaults(func=cmd_cases)

    c = sub.add_parser("recall", help="按话题检索长尾案例，输出可注入块")
    c.add_argument("--intent", required=True, help="当前任务/话题的一句话描述")
    c.add_argument("--with-core", action="store_true",
                   help="连同常驻规则一起输出（默认不重复已在 profile 里的内容）")
    c.set_defaults(func=cmd_recall)

    c = sub.add_parser("promote", help="把达标 rule 升级进常驻档案")
    c.add_argument("--threshold", type=int, default=None)
    c.set_defaults(func=cmd_promote)

    c = sub.add_parser("profile", help="输出常驻档案")
    c.set_defaults(func=cmd_profile)

    c = sub.add_parser("stats", help="统计概览")
    c.set_defaults(func=cmd_stats)

    c = sub.add_parser("pin", help="置顶案例，无视阈值进常驻档案")
    c.add_argument("id")
    c.set_defaults(func=cmd_pin)

    c = sub.add_parser("unpin", help="取消置顶")
    c.add_argument("id")
    c.set_defaults(func=cmd_unpin)

    c = sub.add_parser("forget", help="退役一条案例")
    c.add_argument("id")
    c.set_defaults(func=cmd_forget)

    c = sub.add_parser("viz", help="渲染可视化面板（默认随写操作自动重绘）")
    c.add_argument("--open", action="store_true", help="渲染后用默认浏览器打开")
    c.set_defaults(func=cmd_viz)

    c = sub.add_parser("doctor", help="体检常驻机制（hook 是否挂上、路径是否漂移）")
    c.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc = int(args.func(args) or 0)
    # 写库成功即同步面板：可视化是默认产物，不是可选附加项
    if rc == 0 and args.cmd in MUTATING:
        auto_viz(args.cmd)
    return rc


if __name__ == "__main__":
    sys.exit(main())
