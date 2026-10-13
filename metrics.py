# -*- coding: utf-8 -*-
"""指标层：当日快照 -> 日聚合 -> 跨关键词打分 -> 输出两个文件。

hot_keywords.txt  给下游爬虫消费（每行一个关键词，分数降序）
report_YYYY-MM-DD.md  给人核查（指标明细表 + 异常备注）
"""
import datetime as dt
from pathlib import Path

import config
import db


# ---------- 聚合 ----------

def build_daily_stats(conn, run_date: str) -> None:
    """当日快照聚合进 daily_keyword_stats。空结果词也记一行 0（正常业务态）。

    想要数是抽样采集的（config.DETAIL_SAMPLE_SIZE），总量 = 采样均值 x 在售数。
    关键词排序只需分辨量级差异，估算足够；采样失败（n=0）时 want_total 记 0。
    """
    for word, kw_id in db.get_crawl_words(conn).items():   # active + trial 都要出聚合行
        rows = db.get_snapshot_rows(conn, run_date, kw_id)
        prices = sorted(r["price"] for r in rows if r["price"] is not None)
        nicks = {r["seller_nick"] for r in rows if r["seller_nick"]}
        item_count = len({r["item_id"] for r in rows})
        wants = [r["want_count"] for r in rows if r["want_count"] is not None]
        want_avg = (sum(wants) / len(wants)) if wants else None
        stats = {
            "item_count": item_count,
            "want_total": round(want_avg * item_count) if want_avg is not None else 0,
            "want_sampled_n": len(wants),
            "want_sampled_avg": want_avg,
            "seller_count": len(nicks),
            "price_p25": _percentile(prices, 0.25),
            "price_p50": _percentile(prices, 0.50),
            "price_p75": _percentile(prices, 0.75),
        }
        db.upsert_stats(conn, run_date, kw_id, stats)


# ---------- 打分 ----------

def compute_scores(conn, run_date: str) -> tuple[list[dict], list[dict], dict]:
    """跨关键词打分。

    返回 (scored 降序列表, 零在售词列表, meta)。
    每行附: D供需比 V规模 G增速(可None) 竞争度 各归一化值与最终权重分。
    """
    stat_rows = [dict(r) for r in db.get_stats_rows(conn, run_date)]
    bases = db.get_growth_bases(conn, run_date)
    scored = [r for r in stat_rows if r["item_count"] > 0]
    zeros = [r for r in stat_rows if r["item_count"] == 0]

    for r in scored:
        r["D"] = r["want_total"] / max(r["item_count"], 1)
        r["V"] = r["want_total"]
        r["growth"], r["base_days_ago"] = None, None
        base = bases.get(r["keyword_id"])
        if base:
            base_date, base_want = base
            g = (r["want_total"] - base_want) / max(base_want, 1)
            lo, hi = config.GROWTH_CLIP
            r["growth"] = max(lo, min(hi, g))
            r["base_days_ago"] = (dt.date.fromisoformat(run_date)
                                  - dt.date.fromisoformat(base_date)).days

    # min-max 归一化到 0~1（组内 max==min 时全组记 0.5）
    d_norm = _norm_map([(r["keyword_id"], r["D"]) for r in scored])
    v_norm = _norm_map([(r["keyword_id"], r["V"]) for r in scored])
    g_norm = _norm_map([(r["keyword_id"], r["growth"]) for r in scored
                        if r["growth"] is not None])
    c_norm = _norm_map([(r["keyword_id"], r["item_count"]) for r in scored])

    for r in scored:
        kid = r["keyword_id"]
        # (权重, 归一化值)；竞争度负向取 1-C。缺增速的行权重再归一化（warm-up 天然降级）
        parts = [(config.WEIGHT_DEMAND_SUPPLY, d_norm.get(kid, 0.5)),
                 (config.WEIGHT_VOLUME, v_norm.get(kid, 0.5)),
                 (config.WEIGHT_COMPETITION, 1 - c_norm.get(kid, 0.5))]
        if kid in g_norm:
            parts.append((config.WEIGHT_GROWTH, g_norm[kid]))
        w_sum = sum(w for w, _ in parts)
        r["score"] = round(sum(w * v for w, v in parts) / w_sum, 4)
        r["small_sample"] = r["item_count"] < config.MIN_SAMPLE_SIZE

    scored.sort(key=lambda r: r["score"], reverse=True)

    first_date = db.get_first_collected_date(conn)
    warmup_day = (dt.date.fromisoformat(run_date) - dt.date.fromisoformat(first_date)).days + 1 \
        if first_date else 0
    meta = {"warmup_day": warmup_day,
            "growth_ready": any(r["growth"] is not None for r in scored)}
    return scored, zeros, meta


# ---------- 输出 ----------

def write_outputs(conn, scored: list[dict], zeros: list[dict], meta: dict,
                  run_date: str, out_dir: Path,
                  run_status: str = "", kw_ok: int = 0, kw_total: int = 0,
                  items_saved: int = 0, sb=None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 机器消费：active 词复合分 Top-M（滚雪球出词侧改造：trial 不出品，转正才进榜；
    #    全量透传的老行为保留给 SNOWBALL_ENABLED=False）。种子词带年份旁注的回填
    prefs = db.load_year_prefs(config.KEYWORDS_FILE)
    hot_rows = ([r for r in scored if r.get("state") == "active"][:config.TOP_M_OUTPUT]
                if config.SNOWBALL_ENABLED else scored)
    hot_ids = {id(r) for r in hot_rows}
    hot_path = out_dir / "hot_keywords.txt"
    hot_lines = [
        r["keyword"] + (f"@{prefs[r['keyword']]}" if r["keyword"] in prefs else "")
        for r in hot_rows
    ]
    hot_path.write_text("\n".join(hot_lines) + ("\n" if hot_lines else ""),
                        encoding="utf-8")

    # 2) 人读日报
    lines = [f"# 闲鱼选品日报 {run_date}", ""]
    if run_status:
        lines.append(f"运行: {run_status} | 关键词 {kw_ok}/{kw_total} 成功 | 快照 {items_saved} 条")
    if meta["warmup_day"]:
        if meta["growth_ready"]:
            lines.append(f"冷启动第 {meta['warmup_day']} 天：增速指标已启用")
        else:
            lines.append(f"冷启动第 {meta['warmup_day']} 天：7日增速需历史数据，预计第 8 天起输出")
    lines.append("")

    if scored:
        lines += ["| 排名 | 关键词 | 状态 | 综合分 | 供需比 | 想要总数 | 在售数 | 增速 | 价格P25/P50/P75 | 备注 |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(scored, 1):
            growth = f"{r['growth']:+.0%}" if r["growth"] is not None else "—"
            if r["growth"] is not None and r["base_days_ago"] != 7:
                growth += f"(较{r['base_days_ago']}天前)"
            notes = []
            if r["small_sample"]:
                notes.append("样本少")
            if r["item_count"] > 0 and r["want_sampled_n"] == 0:
                notes.append("想要数采样失败")
            if config.SNOWBALL_ENABLED and r.get("state") == "active" \
                    and r.get("source") != "seed" and id(r) not in hot_ids:
                notes.append("Top-M 外")            # active 但没进今日出品名单
            # 抽样估算的总数加 ~ 前缀，与精确值区分
            want_disp = (f"~{r['want_total']}" if 0 < r["want_sampled_n"] < r["item_count"]
                         else f"{r['want_total']}")
            note = " ".join(notes)
            price = "-/-/-" if r["price_p50"] is None else \
                f"{r['price_p25']:.0f}/{r['price_p50']:.0f}/{r['price_p75']:.0f}"
            lines.append(f"| {i} | {r['keyword']} | {_state_tag(r, run_date)} | {r['score']:.2f} "
                         f"| {r['D']:.1f} | {want_disp} | {r['item_count']} | {growth} "
                         f"| {price} | {note} |")
    else:
        lines.append("（当日无有效在售数据）")
    lines.append("")

    if sb is not None:
        lines += _snowball_section(sb)
        lines.append("")

    if zeros:
        notes = []
        for r in zeros:
            consecutive = _consecutive_zero_days(conn, r["keyword_id"])
            note = f"{r['keyword']}: 0 在售" + (f"，已连续 {consecutive} 天"
                    f"（≥{config.ZERO_RESULT_ALERT_DAYS} 天请人工浏览器复核：疑似词冷或被静默风控）"
                    if consecutive >= config.ZERO_RESULT_ALERT_DAYS else "")
            notes.append(note)
        lines += ["**零在售词**", *[f"- {n}" for n in notes], ""]

    (out_dir / f"report_{run_date}.md").write_text("\n".join(lines), encoding="utf-8")


def _state_tag(row: dict, run_date: str) -> str:
    """榜单状态列：锚点 / trial第N天 / active / retired。"""
    if row.get("source") == "seed":
        return "锚点"
    state = row.get("state") or "active"
    if state == "trial" and row.get("state_since"):
        try:
            n = (dt.date.fromisoformat(run_date)
                 - dt.date.fromisoformat(row["state_since"])).days + 1
        except ValueError:
            n = 1
        return f"trial第{n}天"
    return state


def _snowball_section(sb) -> list[str]:
    """滚雪球段：词池概况 / 新进 trial / 状态机动作 / 门槛淘汰 / 反馈与联想源。"""
    pool = sb.pool or {}
    out = ["## 滚雪球词池",
           f"种子 {pool.get('seed', 0)} · 雪球 active {pool.get('active', 0)}/"
           f"{config.ACTIVE_POOL_CAP} · trial {pool.get('trial', 0)} · "
           f"今日新进 {len(sb.admits)}/{config.MAX_NEW_WORDS_PER_DAY}"
           f" · 修饰词表 {sb.ngram_top_n} 条（ngram_top.txt）"]
    if sb.admits:
        out.append("- 新进 trial: " + "、".join(
            f"{a['word']}({a['source']},分{a['score']:g}"
            + (f",{a['hits']}命中" if a['hits'] else "") + ")" for a in sb.admits))
    for t in sb.transitions:
        out.append(f"- 状态机: {t['word']} {t['from']}→{t['to']}（{t['why']}）")
    if sb.rejects:
        out.append("- 门槛淘汰: " + "、".join(f"{k} {v}" for k, v in
                                               sorted(sb.rejects.items(), key=lambda x: -x[1])))
    for w in (sb.feedback or {}).get("winners", []):
        out.append(f"- 反馈赢家: {w['item_id']} 想要 {w['want_prev']}→{w['want_now']}"
                   f"（标题词加权扩张）")
    if (sb.feedback or {}).get("boosted"):
        out.append(f"- 反馈加权候选 {sb.feedback['boosted']} 个（钱方向优先准入）")
    if sb.suggest_error:
        out.append(f"- ⚠ 联想源降级: {sb.suggest_error}（n-gram 独撑）")
    return out


def _consecutive_zero_days(conn, keyword_id: int) -> int:
    """最近连续 0 在售天数（含今天）。最近 N 天按日期倒序，数开头的连续 0。"""
    consecutive = 0
    for row in db.get_recent_stats(conn, keyword_id):
        if row["item_count"] == 0:
            consecutive += 1
        else:
            break
    return consecutive


def _percentile(sorted_values: list[float], p: float) -> float | None:
    """已排序序列的第 p 分位数（索引切位法）。空序列返回 None。"""
    if not sorted_values:
        return None
    idx = int(p * (len(sorted_values) - 1) + 0.5)
    return sorted_values[idx]


def _norm_map(pairs: list[tuple]) -> dict:
    """[(key, value)] -> {key: 0~1}，min-max 归一化；组内全等记 0.5。"""
    vals = [v for _, v in pairs if v is not None]
    if not vals:
        return {}
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return {k: 0.5 for k, v in pairs if v is not None}
    return {k: (v - lo) / (hi - lo) for k, v in pairs if v is not None}
