# -*- coding: utf-8 -*-
"""滚雪球离线回归（不出网）：挖词 -> 四道门槛 -> 准入 -> 状态机 -> 出品 Top-M。

合成快照里埋好已知市场词，逐日推进验证每个迁移；config 阈值用后还原。
跑法: python test_snowball.py
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

import config
import db
import metrics
import snowball
from fetcher import BaseFetcher
from parser import ItemRecord

TMP = Path(tempfile.mkdtemp(prefix="snowball_test_"))
DB_PATH = TMP / "test.db"
PUBLISH_LOG = TMP / "publish_log.jsonl"

SEEDS = ["PPT模板", "剪辑教程"]


class FakeFetcher(BaseFetcher):
    """search/get_want 滚雪球用不到；suggest 给已知联想词。"""
    def search(self, keyword, page):
        raise NotImplementedError

    def get_want(self, item_id):
        return 1

    def suggest(self, keyword):
        return ["收纳整理好物", "厨房好物分享", "考研网课"]   # 前两个跨市场可进，网课踩黑名单


def rec(iid, title, want):
    return ItemRecord(item_id=iid, title=title, price=1.0, want_count=want,
                      area=None, seller_nick=None, publish_time=None, pic_url=None)


def day1_titles():
    """PPT模板 词下的合成市场：考研英语×12（主力）、AI绘画变现×10（反馈用）、
    网课×3（黑名单）、资料大全×15（纯泛词）。剪辑教程 下垫噪声。"""
    ppt, jj = [], []
    for i in range(12):
        ppt.append(rec(f"a{i:03d}", f"2027考研英语真题手译{i}本", 30 + i))
    for i in range(10):
        ppt.append(rec(f"b{i:03d}", f"AI绘画变现课{i}节", 5))
    for i in range(3):
        ppt.append(rec(f"c{i:03d}", f"考研网课资源{i}", 99))
    for i in range(15):
        ppt.append(rec(f"d{i:03d}", f"考研资料大全{i}", 8))
    for i in range(20):
        jj.append(rec(f"e{i:03d}", f"剪辑教程实战{i}集", 10 + i))
    # 反馈赢家：自家品 b000 前一日想要 10，当日涨到 40（b000 在 b 系列首位 = ppt[12]）
    ppt[12] = rec("b000", "AI绘画变现课0节", 40)
    return {"PPT模板": ppt, "剪辑教程": jj}


def stat_day(word, n_items, want_each):
    """某词一天的搜索结果快照（n_items 条，每条 want_each）。"""
    return [rec(f"{word}-{n_items}-{i}", f"{word}讲义{i}", want_each)
            for i in range(n_items)]


def crawl_and_run(conn, fetcher, day, kw_snapshots: dict):
    """落当日快照 -> 聚合 -> 滚雪球一轮。kw_snapshots = {词: [ItemRecord]}。"""
    crawl_words = db.get_crawl_words(conn)
    for word, items in kw_snapshots.items():
        db.replace_snapshots(conn, day, crawl_words[word], items)
    metrics.build_daily_stats(conn, day)
    return snowball.run(conn, fetcher, day, out_dir=TMP)


def main() -> int:
    os.environ["HOTWORD_SCOUT_PUBLISH_LOG"] = str(PUBLISH_LOG)

    # 状态机/门槛回归固定走字符窗路径（合成语料按字符窗构造，不依赖 jieba 是否安装）
    jieba_backup = config.SNOWBALL_USE_JIEBA
    config.SNOWBALL_USE_JIEBA = False

    conn = db.get_conn(DB_PATH)
    db.init_db(conn)
    db.sync_keywords(conn, SEEDS)
    assert set(db.get_crawl_words(conn)) == set(SEEDS)

    # ---------- Day 1：挖词 + 门槛 + 准入 ----------
    d1 = "2026-10-01"
    db.replace_snapshots(conn, "2026-09-30", db.get_crawl_words(conn)["PPT模板"],
                         [rec("b000", "AI绘画变现课0节", 10)])   # 反馈基线：想要 10
    PUBLISH_LOG.write_text(json.dumps(
        {"ts": f"{d1}T09:00:00", "item_id": "b000", "name": "AI绘画",
         "title": "AI绘画变现课0节", "price": 0.19, "link": "https://x/1"},
        ensure_ascii=False) + "\n", encoding="utf-8")

    sb = crawl_and_run(conn, FakeFetcher(), d1, day1_titles())
    admitted = [a["word"] for a in sb.admits]
    print(f"Day1 admits: {admitted}  rejects: {sb.rejects}  feedback: {sb.feedback}")

    kaoyan = [w for w in admitted if any(k in w for k in ("考研", "英语", "真题", "手译"))]
    assert len(kaoyan) == 1 and len(kaoyan[0]) == 4 \
        and not any(ch.isdigit() for ch in kaoyan[0]), admitted   # 族代表：具体短语
    assert "考研" not in admitted, admitted            # 宽词让位密度更高的具体短语
    assert any("绘画" in w or "变现" in w for w in admitted), admitted  # 反馈加权（族代表词不定）
    assert "收纳整理好物" in admitted, admitted                     # 联想补位
    for w in admitted:                                             # 黑名单绝不进
        assert "网课" not in w, admitted
    assert sb.rejects.get("黑名单", 0) > 0, sb.rejects             # 联想词"考研网课"被挡
    assert sb.feedback["winners"], sb.feedback                     # b000 想要 10 -> 40
    assert len(admitted) <= config.MAX_NEW_WORDS_PER_DAY

    # 四道门槛直接单测（端到端语料未必每道都能踩到）
    g = lambda word, hits=100, pool=(): snowball.gate(
        snowball.Candidate(word=word, source="ngram", score=1.0, hits=hits), list(pool))
    assert g("资料") == "泛词"
    assert g("考") == "词形" and g("考研资料全家桶九九九") == "词形"
    assert g("程实战1") == "词形" and g("考研2027") == "词形"   # CJK+数字混排碎片
    assert g("ps2026") != "词形"                               # 纯 ascii 数字词放行
    assert g("考研网课") == "黑名单"
    assert g("剪辑教程全套", pool=["剪辑教程"]) == "同市场"
    assert g("考研英语", pool=["PPT模板"], hits=3) == "热度不足"
    assert g("小红书运营", pool=["PPT模板"]) == ""

    mods = (TMP / "ngram_top.txt").read_text(encoding="utf-8").splitlines()
    assert mods and any("考研" in m for m in mods), mods[:10]      # 修饰词表落盘

    states = {r["keyword"]: r["state"] for r in db.get_all_words(conn)}
    trial_words = [w for w, s in states.items() if s == "trial"]
    assert set(trial_words) == set(admitted), (trial_words, admitted)

    # ---------- Day 2-4：trial 评估（达标转正 / 不达标退役） ----------
    good = kaoyan[0]                             # 考研市场族代表（词不定，取其索引）
    bad = "收纳整理好物"                        # 联想词当弱 trial：在售 20 < 50 不达标
    for day in ("2026-10-02", "2026-10-03", "2026-10-04"):
        kw_sn = {good: stat_day(good, 55, 8), bad: stat_day(bad, 20, 1)}
        for w in trial_words:
            kw_sn.setdefault(w, stat_day(w, 55, 8))
        sb = crawl_and_run(conn, FakeFetcher(), day, kw_sn)
    trans = {(t["word"], t["to"]) for t in sb.transitions}
    assert (good, "active") in trans, sb.transitions
    assert (bad, "retired") in trans, sb.transitions
    states = {r["keyword"]: (r["state"], r["source"]) for r in db.get_all_words(conn)}
    assert states[good][0] == "active" and states[bad][0] == "retired"
    assert states["PPT模板"] == ("active", "seed")                 # 种子词永不被动机器
    print(f"trial 评估: {good} -> active ✓, {bad} -> retired ✓")

    # ---------- Day 5-9：active 连续 5 天供需比 <1 -> 末位淘汰 ----------
    for day in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"):
        crawl_and_run(conn, FakeFetcher(), day, {good: stat_day(good, 55, 0)})
    states = {r["keyword"]: r["state"] for r in db.get_all_words(conn)}
    assert states[good] == "retired", states[good]
    print(f"active 连续弱势淘汰: {good} -> retired ✓")

    # ---------- 池满末位淘汰 ----------
    cap_backup = config.ACTIVE_POOL_CAP
    config.ACTIVE_POOL_CAP = 1
    try:
        for r in db.get_all_words(conn):                          # 清场：雪球 active 全退役
            if r["state"] == "active" and r["source"] != "seed":
                db.set_word_state(conn, r["keyword_id"], "retired", "2026-10-10")
        words = ["小红书运营", "抖音起号"]
        db.add_snowball_words(conn, [(w, "ngram", "test") for w in words], "2026-10-10")
        for day in ("2026-10-11", "2026-10-12", "2026-10-13"):
            crawl_and_run(conn, FakeFetcher(), day, {w: stat_day(w, 55, 8) for w in words})
        retired = [t for t in sb.transitions] if False else None
        states = {r["keyword"]: r["state"] for r in db.get_all_words(conn)}
        n_active = sum(1 for s in states.values() if s == "active") - len(SEEDS)
        assert n_active <= 1, (n_active, states)                  # cap=1 生效
        assert states["抖音起号"] == "active", states               # 后来者转正
        assert states["小红书运营"] == "retired", states            # 最弱者被挤出
        notes = {r["keyword"]: r["note"] for r in db.get_all_words(conn)}
        assert "让位" in (notes["小红书运营"] or ""), notes
        print(f"池满让位 ✓（雪球 active={n_active} <= cap=1）")
    finally:
        config.ACTIVE_POOL_CAP = cap_backup

    # ---------- 出品：hot_keywords.txt 只出 active Top-M ----------
    scored, zeros, meta = metrics.compute_scores(conn, "2026-10-13")
    metrics.write_outputs(conn, scored, zeros, meta, "2026-10-13", TMP,
                          run_status="success", kw_ok=len(scored), kw_total=len(scored),
                          items_saved=99, sb=sb)
    hot = [ln.strip() for ln in (TMP / "hot_keywords.txt").read_text(encoding="utf-8").splitlines()
           if ln.strip()]
    word_state = {r["keyword"]: r["state"] for r in db.get_all_words(conn)}
    for w in hot:
        assert word_state.get(w) == "active", (w, word_state.get(w))
    assert len(hot) <= config.TOP_M_OUTPUT, hot
    report = (TMP / "report_2026-10-13.md").read_text(encoding="utf-8")
    assert "滚雪球词池" in report and "状态机" in report
    print(f"出品 Top-M: {hot}")

    # ---------- jieba 词级挖掘（装了才跑）：碎片/话术不进 gram，标点数字断链 ----------
    try:
        if snowball.jieba is not None:
            config.SNOWBALL_USE_JIEBA = True       # 本块强制走词级路径（前面机器测试固定字符窗）
            grams = set(snowball._title_grams("AI短剧工作流全流程实操资料"))
            assert "程实操资" not in grams, grams             # 跨词边界碎片（2026-10-14 实踩）
            assert any(("实操" in g or "资料" in g) and len(g) >= 2 for g in grams), grams
            grams2 = set(snowball._title_grams("全流程|实操资料 10月更新"))
            assert all("|" not in g and "10" not in g for g in grams2), grams2
            grams3 = set(snowball._title_grams("不是外面那种烂大街的资料"))
            assert "不是外面" not in grams3 and "不是" not in grams3, grams3  # 话术碎片（词性收尾过滤）
            print("jieba 词级挖掘 ✓")
        else:
            print("jieba 未安装，跳过词级路径（回落字符窗）")
    finally:
        config.SNOWBALL_USE_JIEBA = jieba_backup

    print("\n全部断言通过 ✅")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
