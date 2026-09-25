#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semantic-understanding / viz.py

把 cases.jsonl + profile/expression-profile.md 渲染成一个自包含 HTML 面板
（dashboard/index.html）。零外部依赖、离线可开、单文件。

设计约束（2026-09-25 定）：
  - 分区用 hairline + 留白，不用卡片容器   <- 避免 UI slop
  - 数字一律 tabular-nums 且严格成列
  - 字号极端跳档（64/28/15/11/9），拒绝等差递减
  - 色只承担语义：字面=琥珀（误读），真意=青绿（修正），不用渐变/光晕
  - 交互式 recall 用 JS 复刻 su.py 的打分逻辑，保证线上线下同判准

用法：
  python viz.py                # 渲染
  python viz.py --open         # 渲染并用默认浏览器打开
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime
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
    "min_score": 0.22,
    "inject_top_k": 5,
    "tail_ratio": 0.6,
    "short_prompt_len": 6,
    "fallback_case_id": "c-20260925-005",
}
_FIELDS = ("surface", "literal", "intended", "correction", "rule", "domain", "tags")


# ---------------------------------------------------------------- 基础设施
# 复制自 su.py 的最小子集：viz 不应 import su（避免 CLI/stdout 副作用）

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def storage_path(key: str, fallback: str) -> Path:
    rel = (load_config().get("storage") or {}).get(key, fallback)
    return ROOT / rel


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def core_rule_map() -> dict[str, dict]:
    """从常驻档案解析 core rules -> {rule_text: {...}}，解析失败时返回空字典。"""
    prof_p = storage_path("profile", "profile/expression-profile.md")
    if not prof_p.exists():
        return {}
    text = prof_p.read_text(encoding="utf-8")
    b, e = text.rfind("<!-- BEGIN CORE RULES -->"), text.rfind("<!-- END CORE RULES -->")
    if b < 0 or e < 0 or e < b:
        return {}
    import re
    out: dict[str, dict] = {}
    for line in text[b:e].splitlines():
        m = re.match(r"^\s*-\s+\[(?P<meta>[^\]]*)\]\s*(?P<rest>.+)$", line)
        if not m:
            continue
        meta, rest = m.group("meta"), m.group("rest").strip()
        hits = re.search(r"x(\d+)", meta)
        cid = re.search(r"id:([A-Za-z0-9\-]+)", meta)
        out[rest] = {
            "hits": int(hits.group(1)) if hits else 1,
            "id": cid.group(1) if cid else "",
            "domain": meta.split("·")[0].strip(),
        }
    return out


def esc(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return html.escape(", ".join(str(x) for x in v))
    return html.escape(str(v))


# ---------------------------------------------------------------- 数据整形

def build_payload() -> dict:
    cfg = load_config()
    cases_p = storage_path("cases", "cases/cases.jsonl")
    pending_p = storage_path("pending", "pending/candidates.jsonl")

    all_cases = read_jsonl(cases_p)
    active = [r for r in all_cases if r.get("status") == "active"]
    retired = [r for r in all_cases if r.get("status") != "active"]
    pending = [r for r in read_jsonl(pending_p) if r.get("status") == "pending"]
    core = core_rule_map()

    # 同 rule 归并证据（人间忘出 promote 时也要显示真变异）
    groups: dict[str, list[dict]] = {}
    for r in active:
        groups.setdefault((r.get("rule") or "").strip(), []).append(r)

    threshold = int(cfg.get("promote_threshold", 2))
    rules_out = []
    for rule, members in groups.items():
        if not rule:
            continue
        hits = sum(int(x.get("hits", 1)) for x in members)
        rules_out.append({
            "rule": rule,
            "hits": hits,
            "pinned": any(x.get("pinned") for x in members),
            "core": rule in core or any(x.get("pinned") for x in members),
            "domain": members[0].get("domain", "general"),
            "ids": [x.get("id", "") for x in members],
        })
    rules_out.sort(key=lambda x: (-int(x["core"]), -x["hits"]))

    # rows 先按 jsonl 原始顺序建索引：同分同 hits 时，面板须复刻 su.py 的稳定排序结果
    rows = []
    order = {r.get("id", ""): i for i, r in enumerate(active)}
    for r in active:
        rule = (r.get("rule") or "").strip()
        rows.append({
            "o": order.get(r.get("id", ""), 0),
            "id": r.get("id", ""),
            "ts": r.get("confirmed_ts") or r.get("ts", ""),
            "surface": r.get("surface", ""),
            "literal": r.get("literal", ""),
            "intended": r.get("intended", ""),
            "correction": r.get("correction", ""),
            "rule": rule,
            "domain": r.get("domain", "general"),
            "tags": r.get("tags") or [],
            "hits": int(r.get("hits", 1)),
            "pinned": bool(r.get("pinned")),
            "core": rule in core,
        })
    rows.sort(key=lambda x: (-int(x["core"]), -x["hits"]))

    domains: dict[str, int] = {}
    for r in rows:
        domains[r["domain"]] = domains.get(r["domain"], 0) + 1

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "counts": {
            "cases": len(rows),
            "core": len(core),
            "longtail": len([r for r in rules_out if not r["core"]]),
            "retired": len(retired),
            "pending": len(pending),
            "evidence": sum(r["hits"] for r in rows),
        },
        "threshold": threshold,
        "domains": sorted(domains.items(), key=lambda x: -x[1]),
        "rows": rows,
        "rules": rules_out,
        "cfg": {
            "min_score": cfg.get("min_score", 0.22),
            "top_k": cfg.get("inject_top_k", 5),
            "tail_ratio": cfg.get("tail_ratio", 0.6),
            "short_prompt_len": cfg.get("short_prompt_len", 6),
            "fallback_case_id": cfg.get("fallback_case_id", ""),
        },
    }


# ---------------------------------------------------------------- HTML 模板

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --bg:#0B0B0D; --bg2:#111114; --ink:#E9E9E6; --ink2:#8E8E8A; --ink3:#5C5C58;
  --line:rgba(255,255,255,.09); --line2:rgba(255,255,255,.16);
  --amber:#C98A3E; --teal:#63D6BC; --rose:#E0675F;
  --mono:ui-monospace,"SFMono-Regular",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:"Inter","PingFang SC","Microsoft YaHei","Hiragino Sans GB",system-ui,sans-serif;
}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink)}
body{font-family:var(--sans);font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:0 40px 96px}

/* ---------- gutter：顶部 hairline 元数据条 ---------- */
.gutter{position:sticky;top:0;z-index:20;background:rgba(11,11,13,.92);
  backdrop-filter:blur(12px);border-bottom:1px solid var(--line);
  padding:10px 40px;display:flex;justify-content:space-between;align-items:baseline;
  font-family:var(--mono);font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink3)}
.gutter b{color:var(--ink2);font-weight:500}

/* ---------- hero：极端跳档 + 不对称 ---------- */
.hero{padding:76px 0 40px;display:grid;grid-template-columns:minmax(0,1fr) 232px;gap:48px;align-items:end}
h1{margin:0;font-size:64px;line-height:.94;font-weight:200;letter-spacing:-.03em}
h1 em{font-style:normal;color:var(--teal);font-weight:300}
.sub{margin:16px 0 0;font-size:15px;color:var(--ink2);max-width:52ch;line-height:1.7}
.stamp{font-family:var(--mono);font-size:9px;letter-spacing:.16em;color:var(--ink3);
  text-align:right;text-transform:uppercase;line-height:2.2}
.stamp span{color:var(--ink2)}

/* ---------- 指标：tabular-nums 严格成列 ---------- */
.metrics{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line2);
  border-bottom:1px solid var(--line);padding:26px 0 30px}
.metric{padding:0 24px 0 0;border-left:1px solid var(--line);padding-left:20px}
.metric:first-child{border-left:0;padding-left:0}
.metric .n{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:44px;
  line-height:1;font-weight:300;letter-spacing:-.02em;display:block}
.metric .k{display:block;margin-top:12px;font-family:var(--mono);font-size:9px;
  letter-spacing:.16em;text-transform:uppercase;color:var(--ink3)}
.metric.hot .n{color:var(--teal)}
.metric.wait .n{color:var(--amber)}

/* ---------- section ---------- */
section{margin-top:96px}
.shead{display:flex;align-items:baseline;gap:16px;border-bottom:1px solid var(--line);
  padding-bottom:14px;margin-bottom:0}
.shead h2{margin:0;font-size:15px;font-weight:600;letter-spacing:.02em}
.shead .idx{font-family:var(--mono);font-size:9px;letter-spacing:.18em;color:var(--ink3)}
.shead .note{margin-left:auto;font-family:var(--mono);font-size:9px;letter-spacing:.1em;color:var(--ink3)}

/* ---------- 试算器 ---------- */
.probe{padding:28px 0 0;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:56px}
.field{position:relative}
.field input{width:100%;background:transparent;border:0;border-bottom:1px solid var(--line2);
  color:var(--ink);font-family:var(--sans);font-size:28px;font-weight:200;padding:14px 0 16px;outline:0}
.field input::placeholder{color:var(--ink3)}
.field input:focus{border-bottom-color:var(--teal)}
.field .hint{margin-top:14px;font-size:11px;color:var(--ink3);line-height:1.7}
.presets{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}
.presets button{background:transparent;border:1px solid var(--line2);color:var(--ink2);
  font-family:var(--mono);font-size:10px;letter-spacing:.06em;padding:5px 11px;cursor:pointer;
  border-radius:0;transition:.16s}
.presets button:hover{border-color:var(--teal);color:var(--teal)}

#hits{margin-top:6px;min-height:180px}
.hit{padding:14px 0;border-bottom:1px solid var(--line);display:grid;
  grid-template-columns:44px minmax(0,1fr);gap:16px;align-items:baseline}
.hit .sc{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;color:var(--teal)}
.hit .sc.lo{color:var(--ink3)}
.hit .body{font-size:14px;line-height:1.65}
.hit .meta{font-family:var(--mono);font-size:9px;letter-spacing:.1em;color:var(--ink3);margin-top:5px}
.hit .body b{font-weight:600}
.empty{color:var(--ink3);font-size:13px;padding:22px 0;font-family:var(--mono)}

/* ---------- 错位对照表 ---------- */
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0 6px}
.filters button{background:transparent;border:1px solid var(--line);color:var(--ink2);
  font-family:var(--mono);font-size:10px;letter-spacing:.04em;padding:5px 11px;cursor:pointer;transition:.16s}
.filters button:hover{border-color:var(--line2);color:var(--ink)}
.filters button.on{border-color:var(--teal);color:var(--teal)}

table.map{width:100%;border-collapse:collapse;margin-top:18px}
table.map tr{border-top:1px solid var(--line)}
table.map tr:hover{background:rgba(255,255,255,.022)}
table.map td{padding:22px 20px 22px 0;vertical-align:top}
td.c-lit{width:34%;color:var(--amber);font-size:13px;line-height:1.65;
  text-decoration:line-through;text-decoration-color:rgba(201,138,62,.42)}
td.c-arr{width:26px;color:var(--ink3);font-family:var(--mono);font-size:11px;padding-top:26px}
td.c-int{width:44%;font-size:15px;line-height:1.68;color:var(--ink)}
td.c-meta{width:132px;font-family:var(--mono);font-size:9px;letter-spacing:.09em;
  color:var(--ink3);padding-top:26px;white-space:nowrap}
td.c-rule{display:none}
.ruleline{margin-top:10px;font-family:var(--mono);font-size:10px;line-height:1.7;
  color:var(--ink2);border-left:2px solid var(--line2);padding-left:10px}
.pin{color:var(--teal);border:1px solid rgba(99,214,188,.4);padding:1px 5px;margin-left:6px}
.coreDot{display:inline-block;width:5px;height:5px;background:var(--teal);margin-right:6px;
  vertical-align:middle;border-radius:50%}
.longDot{display:inline-block;width:5px;height:5px;background:var(--ink3);margin-right:6px;
  vertical-align:middle;border-radius:50%}

/* ---------- 证据阶梯 ---------- */
.ladder{margin-top:6px}
.rung{display:grid;grid-template-columns:minmax(0,1fr) 172px 92px;gap:24px;align-items:center;
  padding:13px 0;border-top:1px solid var(--line)}
.rung:last-child{border-bottom:1px solid var(--line)}
.rung .txt{font-size:13px;line-height:1.6;color:var(--ink2)}
.rung.iscore .txt{color:var(--ink)}
.bar{height:3px;background:rgba(255,255,255,.07);position:relative;overflow:visible}
.bar i{position:absolute;left:0;top:0;height:3px;background:var(--ink3);display:block}
.rung.iscore .bar i{background:var(--teal)}
.bar u{position:absolute;top:-4px;width:1px;height:11px;background:var(--rose);opacity:.85}
.rung .tag{font-family:var(--mono);font-size:9px;letter-spacing:.1em;color:var(--ink3);text-align:right}
.rung .tag b{color:var(--teal);font-weight:500}
.rung .tag i{font-style:normal;color:var(--amber)}

/* ---------- domain 条 ---------- */
.domains{margin-top:8px;columns:2;column-gap:56px}
.dom{display:flex;align-items:baseline;gap:12px;padding:9px 0;break-inside:avoid}
.dom .nm{font-family:var(--mono);font-size:10px;letter-spacing:.06em;color:var(--ink2);width:120px;flex:none}
.dom .tr{flex:1;height:1px;background:rgba(255,255,255,.09);position:relative}
.dom .tr i{position:absolute;left:0;top:-1px;height:3px;background:var(--teal);opacity:.72}
.dom .vl{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:11px;color:var(--ink3);width:22px;text-align:right}

footer{margin-top:96px;padding-top:20px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:9px;letter-spacing:.1em;color:var(--ink3);
  display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap}

@media(max-width:900px){
  .wrap{padding:0 20px 72px}.gutter{padding:10px 20px}
  .hero{grid-template-columns:1fr;gap:24px;padding-top:48px}
  h1{font-size:40px}.metrics{grid-template-columns:repeat(2,1fr);gap:22px 0}
  .probe{grid-template-columns:1fr;gap:32px}
  table.map td{display:block;width:auto!important;padding:6px 0}
  td.c-arr,td.c-meta{padding:0}
  .domains{columns:1}
}
"""

JS = r"""
// 打分逻辑必须与 su.py score()/feats() 同判准，否则面板上的数字不可信。
// 判据三层提纯（2026-09-25 重写）：分段取特征 / 丢中文功能字 / 按长度加权。
const FIELD_W = {rule:1.0, tags:0.95, domain:0.8, surface:0.7, intended:0.6, literal:0.25};

const STOP = new Set([...'的地得了着过是在我你他她它们咱这那哪有和与或及不没无别也就都还又再只才而但却可请让把被给对从到往向于之其此等以为因所如若则且并即吗呢吧啊呀么什怎样些个会能要想该应需很太更最说一人二三四五六七八九十']);
const NW = {2:0.45, 3:0.85, 4:1.15}, LATIN_W = 1.30, LATIN_N = 4, LATIN_NW = 0.60;
const CJKc = /[\u4e00-\u9fff]/, LATc = /[a-z0-9_]/;

function segments(t){
  const s = (t||'').toLowerCase(), out = [];
  let cur = '', kind = '';
  for(const ch of s){
    const k = CJKc.test(ch) ? 'c' : (LATc.test(ch) ? 'l' : '');
    if(!k){ if(cur) out.push([kind, cur]); cur = ''; kind = ''; continue; }
    if(k !== kind){ if(cur) out.push([kind, cur]); cur = ch; kind = k; }
    else cur += ch;
  }
  if(cur) out.push([kind, cur]);
  return out;
}
function feats(t){
  const out = {};
  for(const [kind, seg] of segments(t)){
    if(kind === 'l'){
      if(seg.length >= 2) out[seg] = LATIN_W;
      if(seg.length >= LATIN_N)
        for(let i=0;i<=seg.length-LATIN_N;i++){
          const g = seg.substr(i, LATIN_N);
          if(!(g in out)) out[g] = LATIN_NW;
        }
    } else {
      for(const n of [2,3,4]){
        if(seg.length < n) continue;
        for(let i=0;i<=seg.length-n;i++){
          const g = seg.substr(i, n);
          if(g in out) continue;
          let allStop = true;
          for(const c of g){ if(!STOP.has(c)){ allStop = false; break; } }
          if(allStop) continue;
          out[g] = NW[n];
        }
      }
    }
  }
  return out;
}
function fieldText(rec, f){
  const v = rec[f];
  if(v == null) return '';
  return Array.isArray(v) ? v.join(' ') : String(v);
}
function hay(rec){
  return ['surface','intended','rule','domain','literal','tags','correction']
    .map(f=>fieldText(rec,f)).join(' ');
}
function score(rec, q){
  const Q = feats(q);
  const keys = Object.keys(Q);
  if(!keys.length) return 0;
  let qw = 0; for(const k of keys) qw += Q[k];
  let best = 0;
  for(const [f,w] of Object.entries(FIELD_W)){
    const g = feats(fieldText(rec,f));
    let inter = 0;
    for(const k of keys){ if(k in g) inter += Q[k]; }
    if(inter) best = Math.max(best, (inter / qw) * w);
  }
  if(best <= 0) return 0;   // 零重合即 0：旧版会白送 hits*0.01
  const H = hay(rec);
  let bonus = 0;
  for(const k of keys){
    if(k.length >= 3 && H.includes(k)) bonus += Math.min(k.length-1, 4) * 0.045;
  }
  best += Math.min(bonus, 0.45);
  best += Math.min(parseInt(rec.hits||1), 5) * 0.01;
  return Math.min(best, 0.99);
}

const DATA = window.__SU_DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function renderHits(q){
  const box = $('#hits');
  if(!q.trim()){ box.innerHTML = '<div class="empty">// 输入一句话，看它会撞上哪条表意习惯</div>'; return; }
    // 三级排序必须与 su.py recall 的 (-score, -hits) 稳定序完全等价，
    // 否则同一 query 在终端与面板上会出现同分不同名次。
    // 门控必须与 su.py select_hits() 完全等价：硬门槛 -> 领先判据 -> 短句兜底
    let scored = DATA.rows
      .map(r => [score(r, q), r])
      .filter(([s]) => s >= DATA.cfg.min_score)
      .sort((a, b) => b[0]-a[0] || b[1].hits-a[1].hits || a[1].o-b[1].o);
    let fellBack = false;
    if(scored.length){
      const floor = Math.max(DATA.cfg.min_score, scored[0][0] * DATA.cfg.tail_ratio);
      scored = scored.filter(([s]) => s >= floor);
    } else {
      const nCJK = (q.match(/[\u4e00-\u9fff]/g) || []).length;
      const fb = DATA.rows.find(r => r.id === DATA.cfg.fallback_case_id);
      if(fb && nCJK <= DATA.cfg.short_prompt_len){
        scored = [[DATA.cfg.min_score, fb]];
        fellBack = true;
      }
    }
    scored = scored.slice(0, DATA.cfg.top_k);
  if(!scored.length){
    box.innerHTML = '<div class="empty">// 无命中 —— 按 Level 1 协议：动手前先复述一遍你的理解</div>';
    return;
  }
  const head = fellBack
    ? '<div style="font-size:11px;color:var(--amber);font-family:var(--mono);margin-bottom:10px;letter-spacing:.04em">短句兜底 · 字面判据失明，按结构特征兜到「短指令」规则</div>'
    : '';
  box.innerHTML = head + scored.map(([s,r]) => `
    <div class="hit">
      <div class="sc${s<0.3?' lo':''}">${s.toFixed(2)}</div>
      <div class="body">
        他说「<b>${esc(r.surface)}</b>」<span style="color:var(--ink3)"> → </span>实指 ${esc(r.intended)}
        <div class="meta">${r.core?'CORE':'LONG-TAIL'} · ${esc(r.domain)} · x${r.hits} · ${esc(r.id)}</div>
      </div>
    </div>`).join('');
}

function renderTable(filter){
  const rows = filter==='*' ? DATA.rows : DATA.rows.filter(r=>r.domain===filter);
  $('#mapBody').innerHTML = rows.map(r=>`
    <tr data-domain="${esc(r.domain)}">
      <td class="c-lit">${esc(r.literal || r.surface)}</td>
      <td class="c-arr">→</td>
      <td class="c-int">${esc(r.intended)}
        ${r.correction?`<div class="ruleline">纠偏：${esc(r.correction)}</div>`:''}
      </td>
      <td class="c-meta"><span class="${r.core?'coreDot':'longDot'}"></span>${r.core?'CORE':'TAIL'}<br>x${r.hits}${r.pinned?'<span class="pin">PIN</span>':''}<br>${esc(r.domain)}</td>
    </tr>`).join('');
  if(!rows.length) $('#mapBody').innerHTML =
    '<tr><td colspan="4" class="empty">// 该领域暂无案例</td></tr>';
}

function boot(){
  // 试算器
  const inp = $('#q');
  let timer = null;
  inp.addEventListener('input', ()=>{ clearTimeout(timer); timer=setTimeout(()=>renderHits(inp.value),110); });
  inp.addEventListener('keydown', e=>{ if(e.key==='Enter'){ clearTimeout(timer); renderHits(inp.value); } });
  document.querySelectorAll('.presets button').forEach(b=>{
    b.addEventListener('click', ()=>{ inp.value = b.dataset.q; renderHits(inp.value); });
  });
  renderHits('');

  // 领域筛选
  const wrap = $('#filters');
  const doms = ['*', ...DATA.domains.map(d=>d[0])];
  wrap.innerHTML = doms.map((d,i)=>{
    const label = d==='*' ? `全部 ${DATA.counts.cases}` : `${d} ${DATA.domains.find(x=>x[0]===d)[1]}`;
    return `<button data-d="${esc(d)}" class="${i===0?'on':''}">${esc(label)}</button>`;
  }).join('');
  wrap.addEventListener('click', e=>{
    const b = e.target.closest('button'); if(!b) return;
    wrap.querySelectorAll('button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    renderTable(b.dataset.d);
  });
  renderTable('*');
}
document.addEventListener('DOMContentLoaded', boot);
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>表达习惯库 · semantic-understanding</title>
<style>__CSS__</style>
</head>
<body>
<div class="gutter">
  <div><b>su</b> · semantic-understanding · expression profile</div>
  <div>generated __GEN__ · __N__ cases</div>
</div>

<div class="wrap">

  <header class="hero">
    <div>
      <h1>他的话<br>不是<em>那个意思</em></h1>
      <p class="sub">记录「你的原话 → 你的真意」配对，让 AI 越来越懂你的表达。
         下面是这张翻译表的全貌 —— 琥珀划掉的是字面误读，青色是修正后的实指。</p>
    </div>
    <div class="stamp">
      cases <span>__N__</span><br>
      evidence <span>__EV__</span><br>
      domains <span>__ND__</span><br>
      updated <span>__GEN__</span>
    </div>
  </header>

  <div class="metrics">
    <div class="metric hot"><span class="n">__NCORE__</span><span class="k">core rules 常驻</span></div>
    <div class="metric"><span class="n">__NTAIL__</span><span class="k">long-tail 长尾</span></div>
    <div class="metric"><span class="n">__NC__</span><span class="k">cases 案例</span></div>
    <div class="metric__WAITCLS__"><span class="n">__NP__</span><span class="k">pending 待确认</span></div>
  </div>

  <section>
    <div class="shead"><span class="idx">01</span><h2>retrieval probe · 检索试算</h2>
      <span class="note">同 su.py score() 判准 · threshold __MS__</span></div>
    <div class="probe">
      <div>
        <div class="field">
          <input id="q" type="text" autocomplete="off"
                 placeholder="输入一句你要干的事，看它命中哪条习惯">
          <div class="hint">这里跑的是与命令行 <code>su.py recall</code> 完全相同的打分：
            分字段 bigram 覆盖率 + 中文 2~4 字短语字面命中。
            <b style="color:var(--ink2);font-weight:600">分数可在终端复现。</b></div>
        </div>
        <div class="presets">
          __PRESETS__
        </div>
      </div>
      <div id="hits"></div>
    </div>
  </section>

  <section>
    <div class="shead"><span class="idx">02</span><h2>semantic drift · 语义错位对照</h2>
      <span class="note">划掉=字面误读 / 正文=真实意图</span></div>
    <div class="filters" id="filters"></div>
    <table class="map"><tbody id="mapBody"></tbody></table>
  </section>

  <section>
    <div class="shead"><span class="idx">03</span><h2>evidence ladder · 证据阶梯</h2>
      <span class="note">红线=晋升阈值 __TH__ 次 · 过线即入常驻档案</span></div>
    <div class="ladder">
      __LADDER__
    </div>
  </section>

  <section>
    <div class="shead"><span class="idx">04</span><h2>domain spread · 领域分布</h2>
      <span class="note">__ND__ domains</span></div>
    <div class="domains">
      __DOMAINS__
    </div>
  </section>

  <footer>
    <div>semantic-understanding · su.py viz · 自包含单文件，离线可开</div>
    <div>重绘：python su.py viz　|　数据变更（confirm/promote/pin/forget）自动重绘</div>
  </footer>
</div>

<script>window.__SU_DATA__ = __DATA__;</script>
<script>__JS__</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 渲染

def rung_html(data: dict) -> str:
    mx = max([r["hits"] for r in data["rules"]] or [1])
    th = data["threshold"]
    out = []
    for r in data["rules"]:
        w = max(3, int(r["hits"] / max(mx, th + 1) * 100))
        tag_core = "<b>CORE</b>" if r["core"] else "TAIL"
        if r["pinned"] and r["core"]:
            tag_core = "<i>PIN</i> <b>CORE</b>"
        elif r["pinned"]:
            tag_core = "<i>PIN</i> TAIL"
        out.append(
            f'<div class="rung{" iscore" if r["core"] else ""}">'
            f'<div class="txt">{esc(r["rule"])}</div>'
            f'<div class="bar"><i style="width:{w}%"></i>'
            f'<u style="left:{min(100, int(th / max(mx, th + 1) * 100))}%"></u></div>'
            f'<div class="tag">x{r["hits"]} · {tag_core}</div>'
            f"</div>"
        )
    return "\n".join(out) or '<div class="empty">// 案例库为空</div>'


def domains_html(data: dict) -> str:
    mx = max([v for _, v in data["domains"]] or [1])
    return "\n".join(
        f'<div class="dom"><span class="nm">{esc(k)}</span>'
        f'<span class="tr"><i style="width:{int(v/mx*100)}%"></i></span>'
        f'<span class="vl">{v}</span></div>'
        for k, v in data["domains"]
    ) or '<div class="empty">// 无数据</div>'


def render(out_path: Path) -> Path:
    data = build_payload()
    c = data["counts"]

    presets = [
        "弄一下",
        "再想想吧",
        "帮我把这段文案再优化一下",
        "明天要不要带伞",
    ]
    preset_html = "\n".join(
        f'<button data-q="{esc(q)}">{esc(q)}</button>' for q in presets
    )

    html_out = (TEMPLATE
                .replace("__CSS__", CSS)
                .replace("__JS__", JS)
                .replace("__DATA__", json.dumps(data, ensure_ascii=False))
                .replace("__PRESETS__", preset_html)
                .replace("__LADDER__", rung_html(data))
                .replace("__DOMAINS__", domains_html(data))
                .replace("__GEN__", esc(data["generated"]))
                .replace("__TH__", str(data["threshold"]))
                .replace("__MS__", str(data["cfg"]["min_score"]))
                .replace("__WAITCLS__", " wait" if c["pending"] else "")
                .replace("__NCORE__", str(c["core"]))
                .replace("__NTAIL__", str(c["longtail"]))
                .replace("__NC__", str(c["cases"]))
                .replace("__NP__", str(c["pending"]))
                .replace("__N__", str(c["cases"]))
                .replace("__EV__", str(c["evidence"]))
                .replace("__ND__", str(len(data["domains"]))))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")
    return out_path


def default_out() -> Path:
    return ROOT / str((load_config().get("viz") or {}).get("out", "viz/index.html"))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="渲染 semantic-understanding 可视化面板")
    ap.add_argument("--out", default=None)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    out = Path(a.out) if a.out else default_out()
    p = render(out)
    if not a.quiet:
        print(f"[su] 面板已生成：{p}")
        print(f"     {len(json.dumps(build_payload(), ensure_ascii=False))} bytes payload · "
              f"用浏览器打开即可")
    if a.open:
        import webbrowser
        webbrowser.open(p.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
