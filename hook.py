#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semantic-understanding / hook.py

UserPromptSubmit hook —— 每次用户发言时，把命中的表达习惯规则注入上下文。
这是让本技能「真正常驻」的机制：不依赖宿主记得去读，也不依赖触发词命中。

铁律（违反其一即视为事故）：
  1. **绝不阻塞对话**。任何异常 → 静默 exit 0、零输出。hook 坏掉最多是没注入，
     不能变成发言发不出去。
  2. **stdout 只许出现一个 JSON**。任何调试输出走 stderr，否则污染上下文。
  3. **常驻规则不得重复膨胀**：会话内只发一次，另加**与 session 无关的 10 分钟窗口保险**
     （防宿主每轮都换 session_id 导致每轮重发全部规则）。
  4. **长尾命中**按当轮话题现算，但同一组连续命中时不重复注入。
  5. **短句必须兜底**：极短指令靠字面判据必然失明（`改改` 实测 0.00 分），
     此时按长度特征兜到 fallback_case —— 否则「最该对上的那句话」反而全哑。

部署（写入 ~/.workbuddy/settings.json 的 hooks.UserPromptSubmit）：
    "command": "\"<python.exe 绝对路径>\" \"<本文件绝对路径>\""

  · 不要写成 `bash xxx.sh` —— 本机 `bash` 这个名字解析到 C:/Windows/System32/bash.exe
    （WSL 启动器），会被安全策略按 wsl.exe 拦截。本脚本同样不得依赖 coreutils
    （本机 Git Bash 缺 grep/sed/head/dirname）。
  · 解释器写绝对路径，会随 managed Python 版本升级而漂移 → hook 静默失效。
    定期跑 `python su.py doctor` 体检，它会比对配置路径与当前解释器是否一致。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parent
STATE_DIR = SKILL / ".session-state"
STATE_TTL_DAYS = 7
MAX_CONTEXT_CHARS = 2400


# ---------------------------------------------------------------- IO

def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        return {}


def emit(context: str) -> None:
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    sys.stdout.flush()


def safe_sid(sid: str) -> str:
    s = re.sub(r"[^A-Za-z0-9\-_]", "", str(sid or ""))[:80]
    return s or "nosession"


def state_path(sid: str) -> Path:
    return STATE_DIR / f"{safe_sid(sid)}.json"


def load_state(sid: str) -> dict:
    p = state_path(sid)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(sid: str, st: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = state_path(sid).with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path(sid))
        # 顺手清理过期会话状态，避免目录无限增长
        cutoff = time.time() - STATE_TTL_DAYS * 86400
        for f in STATE_DIR.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------- 全局保险
# 风险：若宿主每轮生成的 session_id 都不同，「每会话首次注入」的判断就失效，
# 导致**每一轮都重复塞入全部常驻规则**，上下文被同一段文字持续膨胀。
# 这道保险与 session 无关：常驻规则在任何 CORE_REINJECT_AFTER 秒窗口内只注入一次。
# 窗口到期后允许再注入一次，以覆盖长会话中上下文被压缩的情况。

GLOBAL_STATE = STATE_DIR / "_global.json"
CORE_REINJECT_AFTER = 600


def load_global() -> dict:
    try:
        if GLOBAL_STATE.exists():
            return json.loads(GLOBAL_STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_global(d: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = GLOBAL_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(GLOBAL_STATE)
    except Exception:
        pass


def core_recently_injected() -> bool:
    last = float(load_global().get("last_core_ts") or 0)
    return (time.time() - last) < CORE_REINJECT_AFTER


# ---------------------------------------------------------------- 主逻辑

def should_skip(prompt: str) -> bool:
    """只跳过两类：空输入、slash 命令。
    注意**不按长度过滤** —— 越短的指令越依赖语义对齐
    （「别推。别覆盖。」这种正是最该命中的）。"""
    p = prompt.strip()
    if not p:
        return True
    if p.startswith("/"):
        return True
    return False


def build_context(prompt: str, sid: str) -> str:
    sys.path.insert(0, str(SKILL))
    import su  # 延迟导入：hook 进程只做一次

    cfg = su.load_config()
    cases_p = su.storage_path("cases", "cases/cases.jsonl")
    rows = [r for r in su.read_jsonl(cases_p) if r.get("status") == "active"]

    # 门控统一走 su.select_hits：硬门槛 + 领先判据 + 短句兜底。
    # 三处（recall / hook / 面板）必须同判准，否则会出现
    # 「终端说命中、hook 不注入」这类静默漂移。
    hits, fell_back = su.select_hits(rows, prompt, cfg)
    top = hits[: int(cfg.get("inject_top_k", 5))]

    core = su.core_rules()
    core_ids = {cid for _, _, _, cid in core}

    st = load_state(sid)
    first_of_session = not st.get("core_sent")
    # 双条件：本会话内没发过 + 全局窗口内没刚发过。
    # 后者是防 session_id 每轮变化的那道保险，见上方注释。
    want_core = bool(core) and first_of_session and not core_recently_injected()

    blocks: list[str] = []

    if want_core:
        lines = ["**他的表达习惯 · 常驻规则（优先于字面理解，不必复述）**"]
        for text, hits, domain, cid in core[: int(cfg.get("max_profile_rules", 20))]:
            lines.append(f"- [{domain}] {text}")
        blocks.append("\n".join(lines))
        st["core_sent"] = True
        st["started"] = st.get("started") or time.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(sid, st)
        save_global({"last_core_ts": time.time()})

    tail = [(s, r) for s, r in top if r.get("id") not in core_ids]
    tail_ids = [str(r.get("id")) for _, r in tail]
    # 同一组长尾连续命中时不重复注入 —— 规则已在上下文里，再塞一遍只是噪音。
    # 换了话题、命中换了一组，才重新注入。
    if tail and tail_ids != (st.get("last_tail") or []):
        lines = (["**这句话太短，字面判据必然失明 —— 按结构特征兜到「短指令」规则**"]
                 if fell_back else ["**本次话题相关的长尾命中**"])
        for s, r in tail:
            say = str(r.get("surface", ""))[:70]
            rule = r.get("rule") or r.get("intended", "")
            lines.append(f"- 他说「{say}」→ {rule}")
        blocks.append("\n".join(lines))
        st["last_tail"] = tail_ids
        save_state(sid, st)

    if not blocks:
        return ""

    blocks.append("> 以上优先于字面理解。没覆盖到的说法，动手前一行复述你的理解；"
                  "写文件 / push / 删除这类不可逆动作必须复述。")
    ctx = "\n\n".join(blocks)
    return ctx[:MAX_CONTEXT_CHARS]


def main() -> int:
    payload = read_payload()
    prompt = payload.get("prompt") or ""
    if should_skip(prompt):
        return 0

    sid = payload.get("session_id") or "nosession"
    ctx = build_context(prompt, sid)
    if ctx:
        emit(ctx)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 静默放行：hook 永不阻塞对话
        print(f"[su-hook] 注入失败，已放行：{e}", file=sys.stderr)
        sys.exit(0)
