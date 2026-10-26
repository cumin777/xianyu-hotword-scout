# -*- coding: utf-8 -*-
"""滚雪球自动选词（PLAN #7）：自动找词 + 自动挑词，人工只剩锚点词 + 看日报。

三源候选：
  ① 快照标题 n-gram（主力，零新增协议）：当日全部快照标题切 2~4 字滑窗，
     按想要数加权——高需求子市场的词组会在标题里反复出现
  ② 搜索联想：买家下拉真实搜索词（fetcher.suggest，只对当日榜单前 N 词调，
     每词 +1 个 mtop 请求；失败自动降级，n-gram 独撑）
  ③ 自家品反馈闭环：publish_log 的 itemId 与当日快照交叉，想要数上涨的品
     其标题词加权扩张（雪往钱多处滚；出单信号等真实订单记录出现后接入）

四道门槛（候选 -> trial）：
  ① 词形 2~8 字且去泛词（"资料/教程"剥掉后必须还有内容字）
  ② 平台红线黑名单（substring 命中即弃）
  ③ 同市场去重：与词池任何词同市场即弃（含 retired，防退役词反复回炉）——
     "考研"与"考研资料"只留强的，口径同 publisher copywriter 的 _keyword_matches
  ④ 热度证据：n-gram 须出现在 >= NGRAM_MIN_HITS 个不同商品标题（联想词是
     买家真实输入，自带热度，天然豁免）；在售数 >= 50 这道在 trial 评估时用
     真实采集数据判——候选期只有标题，在售数是搜索后才有的量

词池状态机（每日采集+聚合后跑一轮，入口 run()）：
  trial 攒满 TRIAL_DAYS 个有数据的天 -> 日均在售 >= TRIAL_MIN_ITEMS 且
     日均供需比 >= TRIAL_MIN_RATIO -> active（雪球池满则末位淘汰最弱者让位）；
     否则 retired
  active 连续 ACTIVE_BAD_DAYS 天供需比 < ACTIVE_BAD_RATIO -> retired
  种子词（source=seed）不进状态机、不占 ACTIVE_POOL_CAP、不可被淘汰
"""
import difflib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
import db
from fetcher import BaseFetcher

try:
    import jieba                                  # 可选依赖：词级挖掘（2026-10-14 实测
    import jieba.posseg as _pseg                  # 字符窗 5 进 5 全是跨词碎片，装它治本）
except ImportError:
    jieba = None
    _pseg = None


def _flag_ok(flag: str) -> bool:
    """市场词必是名词性收尾（教程/资料/工作流/brushset）：n*/v*/a*/eng 放行，
    副词/方位/连词/代词收尾（"不是外面""怎么用"）一律不是市场词。"""
    return flag.startswith(("n", "v", "a")) or flag == "eng"


# ---------- 词形/市场口径（与 publisher copywriter 同源语义，独立实现保持开源自包含） ----------

_SEP_RE = re.compile(r"[\s\-_/，。·.、+【】\[\]()（）!！?？~～*#@\"'“”‘’|｜;；:：]+")
_CJK_RE = re.compile(r"[一-鿿]")
_ALPHA_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"[0-9]")


def norm_word(s: str) -> str:
    """小写 + 去分隔符——词形与市场比较的统一底稿。"""
    return _SEP_RE.sub("", s).lower()


def strip_generic(s: str) -> str:
    for w in config.GENERIC_WORDS:
        s = s.replace(w, "")
    return s


def same_market(a: str, b: str) -> bool:
    """两词是否同一市场：互为子串；或原始串存在 >=2 字公共子串且该段去泛词后非空。

    公共段先看原始串（碎片会错位："辑教程实"~"剪辑教程" 的重叠"辑教程"跨在泛词
    "教程"上，先剥泛词会丢掉重叠），再用"公共段剥泛词非空"挡住纯泛词重叠
    （"考研资料"~"高考资料" 只共享"资料"->剥后为空，不同市场）。
    """
    na, nb = norm_word(a), norm_word(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    for m in difflib.SequenceMatcher(None, na, nb).get_matching_blocks():
        if m.size >= 2 and strip_generic(na[m.a:m.a + m.size]):
            return True
    return False


# ---------- 数据结构 ----------

@dataclass
class Candidate:
    word: str
    source: str            # ngram / suggest
    score: float           # 排序分（n-gram = 想要数加权和；suggest = 1.0 名义分）
    hits: int = 0          # 出现的不同商品数（热度证据）
    tier: int = 2          # 准入优先级：1=反馈加权的钱方向，2=n-gram 按分，3=联想补位
    ids: frozenset | None = None   # n-gram 命中商品集（包含关系聚类用，suggest 无）
    reason: str = ""       # 门槛拒绝原因（rejected 时填）


@dataclass
class SnowballSummary:
    transitions: list = field(default_factory=list)   # 状态机动作 {word,from,to,why}
    admits: list = field(default_factory=list)        # 今日新进 trial {word,source,score,hits}
    rejects: dict = field(default_factory=dict)       # 原因 -> 数量（只计数，词多不逐一列）
    pool: dict = field(default_factory=dict)          # {seed, active, trial}
    feedback: dict = field(default_factory=dict)      # {winners: [...], boosted: n}
    suggest_error: str = ""                           # 联想源降级原因（空 = 正常）
    ngram_top_n: int = 0                              # ngram_top.txt 条数


# ---------- 源①：快照标题 n-gram 挖掘 ----------

def _iter_ngrams(text: str):
    for n in range(config.NGRAM_MIN_LEN, config.NGRAM_MAX_LEN + 1):
        for i in range(len(text) - n + 1):
            yield text[i:i + n]


def _title_grams(title: str):
    """标题 -> gram 迭代器。

    装了 jieba 走词级：1~3 个连续词拼合（总长 2~8 字），标点/纯数字 token 断链
    ——从根上避开"程实操资/月蕞新a"式跨词边界碎片（2026-10-14 首日实测：字符窗
    5 进 5 全是碎片，滚雪球空转）。没装则回落 2~4 字符滑窗（零依赖承诺不破）。
    """
    if config.SNOWBALL_USE_JIEBA and jieba is not None:
        # 词级 gram：1~3 个连续词拼合（总长 2~8）。单字 token（jieba 对"工作流全流程"
        # 这类连排会吐单字）与数字/标点一样断链——拼进去会再造"程实操资"式伪词；
        # 收尾 token 词性不过 _flag_ok 的不产 gram（"不是外面"式话术碎片）。
        if _pseg is not None:
            toks = [None if len(w) < 2 or w.isdigit()
                    or not (_CJK_RE.search(w) or _ALPHA_RE.search(w))
                    else (w.lower(), f) for w, f in _pseg.cut(title)]
        else:
            toks = [None if len(t) < 2 or t.isdigit()
                    or not (_CJK_RE.search(t) or _ALPHA_RE.search(t))
                    else (t.lower(), "n") for t in jieba.cut(title)]
        for i, t in enumerate(toks):
            if t is None:
                continue
            gram = ""
            for j in range(i, min(i + 3, len(toks))):    # 最多 3 个连续词
                if toks[j] is None:
                    break
                gram += toks[j][0]
                if len(gram) > config.CAND_MAX_CHARS:
                    break
                if len(gram) >= config.CAND_MIN_CHARS and _flag_ok(toks[j][1]):
                    yield gram
    else:
        t = norm_word(title)
        if len(t) >= config.NGRAM_MIN_LEN:
            yield from _iter_ngrams(t)


def mine_ngrams(conn, run_date: str, winners: set[str] | None = None
                ) -> tuple[list[Candidate], list[tuple[str, float, int]], dict]:
    """当日快照标题 -> (候选列表, 高频修饰词表, 门槛前剔除计数)。

    候选按"命中商品集"聚类（同市场判据对同短语碎片失灵）：同集=同短语族只留代表
    （字多/无数字/字典序，显式定序——set 哈希序跨运行不稳）。四道门槛与计数在
    admit 侧 gate 统一做；此处的 pre_rejects 只收三类结构性剔除：热度不足
    （<NGRAM_MIN_HITS 商品命中）、跨市场（命中横跨 >=CAND_MAX_KEYWORD_SPREAD 个
    在爬词 = "自动发货/烂大街的"式通用语，不是市场词）、修饰词不受跨市场过滤
    （通用语恰是好的标题修饰词）。
    winners = 反馈命中的自家品标题集合：候选词出现在其中 -> tier=1（钱方向优先）。
    """
    rows = [(r["item_id"], r["title"], r["want_count"], r["keyword_id"])
            for r in db.get_titles_for_date(conn, run_date)]
    want_map = {item_id: want for item_id, _, want, _ in rows}
    kw_map = {item_id: kw_id for item_id, _, _, kw_id in rows}
    hits: dict[str, set] = {}
    for item_id, title, _, _ in rows:
        for gram in set(_title_grams(title)):
            hits.setdefault(gram, set()).add(item_id)

    winners = winners or set()
    # ---- 修饰词表（宽口径，独立于候选门槛；不做跨市场过滤） ----
    modifiers: list[tuple[str, float, int]] = []
    for gram, ids in hits.items():
        if gram.isdigit() or (not _CJK_RE.search(gram) and not _ALPHA_RE.search(gram)):
            continue
        if any(b in gram for b in config.BLACKLIST):
            continue
        if len(gram) <= config.CAND_MAX_CHARS and len(ids) >= max(2, config.NGRAM_MIN_HITS // 2):
            modifiers.append((gram, float(sum(want_map.get(i) or 0 for i in ids)), len(ids)))
    modifiers = _dedup_modifiers(modifiers)
    modifiers.sort(key=lambda m: (-m[1], -len(m[0])))

    # ---- 候选（窄口径）：命中集聚类 -> 族代表 ----
    families: dict[frozenset, list[str]] = {}
    pre_rejects = {"热度不足": 0, "跨市场": 0}
    for gram, ids in hits.items():
        if gram.isdigit() or (not _CJK_RE.search(gram) and not _ALPHA_RE.search(gram)):
            continue
        if len(ids) < config.NGRAM_MIN_HITS:
            pre_rejects["热度不足"] += 1
            continue
        if len({kw_map[i] for i in ids}) >= config.CAND_MAX_KEYWORD_SPREAD:
            pre_rejects["跨市场"] += 1
            continue
        families.setdefault(frozenset(ids), []).append(gram)
    cands: list[Candidate] = []
    for ids, grams in families.items():
        score = float(sum(want_map.get(i) or 0 for i in ids))
        best = max(grams, key=lambda g: (len(g), _DIGIT_RE.search(g) is None, g))
        cands.append(Candidate(word=best, source="ngram", score=score, hits=len(ids),
                               ids=frozenset(ids),
                               tier=1 if any(best in w for w in winners) else 2))
    return cands, modifiers[:config.NGRAM_TOP_N], pre_rejects


def _dedup_candidates(cands: list[Candidate]) -> list[Candidate]:
    """同市场簇内只留一个：tier 小者胜（钱方向优先），并列留更具体的（字多），
    再比想要数加权分——宽词("曝光")分数天然高于其市场短语("闲鱼曝光")，
    按分排序会系统性地选宽弃专，与"只留强的"意图相反。"""
    keep: list[Candidate] = []
    for c in cands:
        for k in keep:
            if same_market(c.word, k.word):
                if (c.tier, -len(c.word), -c.score) < (k.tier, -len(k.word), -k.score):
                    k.word, k.score, k.hits, k.source, k.tier = \
                        c.word, c.score, c.hits, c.source, c.tier
                break
        else:
            keep.append(c)
    return keep


def _dedup_modifiers(mods: list[tuple]) -> list[tuple]:
    keep: list[tuple] = []
    for m in mods:
        for i, k in enumerate(keep):
            if same_market(m[0], k[0]):
                if (len(m[0]), m[1]) > (len(k[0]), k[1]):   # 具体词优先（字多），同长比分
                    keep[i] = m
                break
        else:
            keep.append(m)
    return keep


# ---------- 源②：搜索联想 ----------

def suggest_candidates(conn, fetcher: BaseFetcher, run_date: str
                       ) -> tuple[list[Candidate], str]:
    """对当日榜单前 N 词调联想接口，收集买家真实搜索词。失败返回 (已收集的, 原因)。
    联想词没有想要数权重：score 名义分 1.0、tier=3 候选补位、热度门槛豁免。"""
    if not config.SUGGEST_ENABLED:
        return [], "off"
    top_words = conn.execute(
        "SELECT k.keyword FROM daily_keyword_stats s JOIN keywords k USING(keyword_id) "
        "WHERE s.crawl_date=? AND s.item_count>=? AND k.state='active' "
        "ORDER BY CAST(s.want_total AS REAL)/MAX(s.item_count,1) DESC LIMIT ?",
        (run_date, config.MIN_SAMPLE_SIZE, config.SUGGEST_FOR_TOP_N),
    ).fetchall()
    out: list[Candidate] = []
    for i, r in enumerate(top_words):
        try:
            words = fetcher.suggest(r["keyword"])
        except Exception as e:                      # 联想挂了不该毁整个滚雪球
            return out, f"{type(e).__name__}: {e}"
        out.extend(Candidate(word=w, source="suggest", score=1.0, tier=3)
                   for w in (norm_word(x) for x in words) if w)
        if i + 1 < len(top_words):
            time.sleep(random.uniform(config.SLEEP_MIN, config.SLEEP_MAX))
    return out, ""


# ---------- 源③：自家品反馈 ----------

def feedback_winners(conn, run_date: str) -> dict:
    """publish_log 自家品与快照交叉：想要数上涨的品 = 赢家，其标题进加权池。

    出单信号（比想要数涨更硬的钱信号）等 trader 真实订单落盘后接入——
    publish_log 是 publisher 侧台账，订单在 trader 侧，目前没有结构化文件。
    """
    info: dict = {"winners": [], "titles": set()}
    path = _publish_log_path()
    if path is None:
        return info
    cutoff = time.time() - config.FEEDBACK_MAX_AGE_DAYS * 86400
    ours: list[tuple[str, str]] = []                 # (item_id, 我们的标题)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return info
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        try:
            fresh = time.mktime(time.strptime(str(rec.get("ts", ""))[:19],
                                              "%Y-%m-%dT%H:%M:%S")) >= cutoff
        except ValueError:
            fresh = False
        if fresh and rec.get("item_id") and rec.get("title"):
            ours.append((str(rec["item_id"]), rec["title"]))
    for item_id, title in ours:
        hist = db.get_item_wants(conn, item_id)      # [(date, want)] 日期倒序
        if len(hist) < 2:
            continue                                 # 没有跨天对比就没有"涨"
        (d_now, w_now), (d_prev, w_prev) = hist[0], hist[1]
        if d_now != run_date or w_now is None or w_prev is None:
            continue                                 # 今日没进快照前2页（无信号）
        if w_now > w_prev:
            info["winners"].append({"item_id": item_id, "title": title,
                                    "want_prev": w_prev, "want_now": w_now})
            info["titles"].add(norm_word(title))
    return info


def _publish_log_path() -> Path | None:
    """自家品台账：环境变量 > config 直配 > RUNTIME_DIR 兄弟目录（pipeline 布局）。
    独立使用本仓库（无 publisher）时三者皆空 = 反馈源关闭，其余源不受影响。"""
    for p in (os.environ.get("HOTWORD_SCOUT_PUBLISH_LOG"), config.FEEDBACK_PUBLISH_LOG):
        if p:
            path = Path(p)
            return path if path.exists() else None
    sibling = config.RUNTIME_DIR.parent / "publisher" / "publish_log.jsonl"
    return sibling if sibling.exists() else None


# ---------- 门槛与准入 ----------

def gate(candidate: Candidate, pool_words: list[str]) -> str:
    """四道门槛（同市场去重对全词池含 retired）。返回 "" = 通过，否则拒绝原因。"""
    w = norm_word(candidate.word)
    if not config.CAND_MIN_CHARS <= len(w) <= config.CAND_MAX_CHARS or w.isdigit():
        return "词形"
    if _CJK_RE.search(w) and _DIGIT_RE.search(w):
        return "词形"          # CJK+数字混排（"程实战1"/"考研2027"）是标题碎片不是搜索词；
                               # 纯 ascii 数字词（ps2026/c4d）是真搜索词，放行
    if not strip_generic(w):
        return "泛词"
    if any(b in w for b in config.BLACKLIST):
        return "黑名单"
    if any(same_market(w, p) for p in pool_words):
        return "同市场"
    if candidate.source == "ngram" and candidate.hits < config.NGRAM_MIN_HITS:
        return "热度不足"
    return ""


def admit(conn, run_date: str, fetcher: BaseFetcher,
          out_dir: Path | None = None) -> SnowballSummary:
    """三源汇总 -> 四道门槛 -> 同市场再排重 -> 排序准入（每日 <= MAX_NEW_WORDS_PER_DAY
    词进 trial）。tier=1（反馈加权）优先于 n-gram 分数优先于联想补位。"""
    s = SnowballSummary()
    pool_words = [r["keyword"] for r in db.get_all_words(conn)]

    fb = feedback_winners(conn, run_date)
    s.feedback = {"winners": fb["winners"], "boosted": 0}

    ngram_cands, modifiers, pre_rejects = mine_ngrams(conn, run_date, winners=fb["titles"])
    for reason, n in pre_rejects.items():           # mine 侧聚类剔除（热度不足/子集弱弃）
        s.rejects[reason] = s.rejects.get(reason, 0) + n
    suggest_cands, s.suggest_error = suggest_candidates(conn, fetcher, run_date)
    for c in suggest_cands:                         # 联想词踩中赢家标题也升钱方向
        if any(c.word in w for w in fb["titles"]):
            c.tier = 1

    # 修饰词表不受挖掘开关影响（publisher copywriter 标题长尾还吃它，停了 7 天就过期失联）
    write_ngram_top(modifiers, out_dir or config.RUNTIME_DIR)
    s.ngram_top_n = len(modifiers)
    if not config.SNOWBALL_ADMIT_NEW:
        s.rejects["挖掘关闭"] = len(ngram_cands) + len(suggest_cands)
        return s

    passed: list[Candidate] = []
    for c in ngram_cands + suggest_cands:
        reason = gate(c, pool_words)
        if reason:
            s.rejects[reason] = s.rejects.get(reason, 0) + 1
        else:
            passed.append(c)
    s.feedback["boosted"] = sum(1 for c in passed if c.tier == 1)

    # 命中集包含关系聚类：A 的命中商品全都也含 B = 词形判据看不见的同市场
    # （"英语真题"族与"考研"族互不含公共 2 字，但前者命中集 ⊂ 后者）。
    # 簇内留"密度"高者（score/hits，想要数强度），不再按覆盖广度——宽词分数
    # 天然更高，按分留会系统性选宽弃专；钱方向（tier）优先于密度。
    passed.sort(key=lambda c: (c.tier, -(c.score / max(c.hits, 1))))
    kept: list[Candidate] = []
    for c in passed:
        if c.ids is not None and any(
                k.ids is not None and (c.ids <= k.ids or k.ids <= c.ids) for k in kept):
            s.rejects["同市场"] = s.rejects.get("同市场", 0) + 1
            continue
        kept.append(c)

    passed = _dedup_candidates(kept)                # 词形同市场簇内留"强且具体"的一个
    passed.sort(key=lambda c: (c.tier, -len(c.word), -c.score))
    chosen = passed[:config.MAX_NEW_WORDS_PER_DAY]
    inserted = db.add_snowball_words(
        conn, [(c.word, c.source, f"score={c.score:g} hits={c.hits} tier={c.tier}"
                + (" 反馈加权" if c.tier == 1 else "")) for c in chosen], run_date)
    s.admits = [{"word": w, "source": c.source, "score": c.score, "hits": c.hits}
                for w, c in zip(inserted, chosen)]
    return s


def write_ngram_top(modifiers: list[tuple[str, float, int]], out_dir: Path) -> None:
    """高频修饰词表（喂 publisher copywriter 标题长尾；纯词每行一个，机器消费格式）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / config.NGRAM_TOP_PATH.name
    path.write_text("\n".join(m[0] for m in modifiers) + ("\n" if modifiers else ""),
                    encoding="utf-8")


# ---------- 状态机 ----------

def _ratio(item_count: int, want_total: int) -> float:
    return want_total / max(item_count, 1)


def state_machine(conn, run_date: str) -> list[dict]:
    """trial 评估转正/淘汰 + active 末位淘汰。种子词不参与。返回动作列表（日报用）。"""
    actions: list[dict] = []
    pool = [dict(r) for r in db.get_all_words(conn)]

    # 1) trial：攒满 TRIAL_DAYS 个有数据的天 -> 评估（state_since 起算）
    for w in pool:
        if w["state"] != "trial":
            continue
        rows = db.get_stats_since(conn, w["keyword_id"], w["state_since"] or "2000-01-01")
        if len(rows) < config.TRIAL_DAYS:
            continue
        avg_items = sum(r["item_count"] for r in rows) / len(rows)
        avg_ratio = sum(_ratio(r["item_count"], r["want_total"]) for r in rows) / len(rows)
        if avg_items >= config.TRIAL_MIN_ITEMS and avg_ratio >= config.TRIAL_MIN_RATIO:
            evicted = _evict_if_full(conn, run_date, actions)
            db.set_word_state(conn, w["keyword_id"], "active", run_date,
                              note=f"trial 达标: 日均在售{avg_items:.0f} 供需比{avg_ratio:.1f}")
            actions.append({"word": w["keyword"], "from": "trial", "to": "active",
                            "why": f"日均在售 {avg_items:.0f} / 供需比 {avg_ratio:.1f}"
                            + (f"，挤出 {evicted}" if evicted else "")})
        else:
            db.set_word_state(conn, w["keyword_id"], "retired", run_date,
                              note=f"trial 不达标: 日均在售{avg_items:.0f} 供需比{avg_ratio:.1f}")
            actions.append({"word": w["keyword"], "from": "trial", "to": "retired",
                            "why": f"日均在售 {avg_items:.0f} / 供需比 {avg_ratio:.1f}"})

    # 2) active：连续 N 天供需比 < 阈值 -> 末位淘汰（今日无数据不评，熔断日不冤杀）
    for w in pool:
        if w["state"] != "active" or w["source"] == "seed":
            continue
        rows = db.get_recent_stats(conn, w["keyword_id"], days=config.ACTIVE_BAD_DAYS)
        if not rows or rows[0]["crawl_date"] != run_date:
            continue
        bad = 0
        for r in rows:
            if _ratio(r["item_count"], r["want_total"]) < config.ACTIVE_BAD_RATIO:
                bad += 1
            else:
                break
        if bad >= config.ACTIVE_BAD_DAYS:
            db.set_word_state(conn, w["keyword_id"], "retired", run_date,
                              note=f"连续{bad}天供需比<{config.ACTIVE_BAD_RATIO:g}")
            actions.append({"word": w["keyword"], "from": "active", "to": "retired",
                            "why": f"连续 {bad} 天供需比 < {config.ACTIVE_BAD_RATIO:g}"})
    return actions


def _evict_if_full(conn, run_date: str, actions: list[dict]) -> str:
    """雪球 active 池满 -> 淘汰最弱者（近 3 天日均供需比最低，无数据=最弱）让位。
    返回被挤出词名（池未满返回 ""）。种子词永不占池、永不被挤。"""
    actives = db.get_snowball_actives(conn)
    if len(actives) < config.ACTIVE_POOL_CAP:
        return ""
    weakest, weak_ratio = None, float("inf")
    for r in actives:
        rows = db.get_recent_stats(conn, r["keyword_id"], days=3)
        ratio = (sum(_ratio(x["item_count"], x["want_total"]) for x in rows) / len(rows)
                 if rows else -1.0)
        if ratio < weak_ratio:
            weakest, weak_ratio = r, ratio
    if weakest is None:
        return ""
    db.set_word_state(conn, weakest["keyword_id"], "retired", run_date,
                      note=f"池满({config.ACTIVE_POOL_CAP})让位，近3天供需比{weak_ratio:.2f}")
    actions.append({"word": weakest["keyword"], "from": "active", "to": "retired",
                    "why": f"池满末位让位（近3天供需比 {weak_ratio:.2f}）"})
    return weakest["keyword"]


# ---------- 每日一轮 ----------

def run(conn, fetcher: BaseFetcher, run_date: str, out_dir: Path | None = None
        ) -> SnowballSummary:
    """采集+聚合之后调用：状态机迁移 -> 挖词准入 -> 修饰词表。异常由调用方捕获降级。

    顺序：先状态机再准入——今日退役的词腾出的市场，当日候选即可顶上。
    """
    transitions = state_machine(conn, run_date)
    summary = admit(conn, run_date, fetcher, out_dir)
    summary.transitions = transitions
    pool_rows = db.get_all_words(conn)
    summary.pool = {
        "seed": sum(1 for r in pool_rows if r["source"] == "seed" and r["state"] == "active"),
        "active": sum(1 for r in pool_rows
                      if r["source"] != "seed" and r["state"] == "active"),
        "trial": sum(1 for r in pool_rows if r["state"] == "trial"),
    }
    return summary
