# -*- coding: utf-8 -*-
"""管路联测（临时）：合成响应 -> 采集 -> 入库 -> 聚合 -> 打分 -> 输出，全链路不出网。
不占用 runtime/sample_search.json（那是留给真实响应的）。"""
import json
import shutil
from pathlib import Path

import config
import crawler
import db
import metrics
from fetcher import MockFetcher

MOCK_DIR = config.RUNTIME_DIR / "mock"
SYNTH = MOCK_DIR / "sample_synthetic.json"


def make_synthetic():
    """按文档结构造 2 条商品的搜索响应"""
    def item(iid, title, price, want, nick):
        return {"data": {"item": {"main": {
            "exContent": {"itemId": iid, "title": title,
                          "price": [{"text": "¥"}, {"text": price}],
                          "want": f"{want}人想要", "userNickName": nick,
                          "area": "上海", "publishTime": "2026-10-01",
                          "picUrl": f"http://x/{iid}.jpg"},
            "clickParam": {"args": {"wantNum": str(want), "item_id": iid}},
        }}}}
    return {"ret": ["SUCCESS::调用成功"],
            "data": {"resultList": [item("1001", "考研资料全家桶2027", "29.9", 35, "卖家A"),
                                    item("1002", "考研英语真题手译", "9.9", 5, "卖家B")]}}


def main():
    if MOCK_DIR.exists():
        shutil.rmtree(MOCK_DIR)      # 干净重来
    MOCK_DIR.mkdir(parents=True)
    SYNTH.write_text(json.dumps(make_synthetic(), ensure_ascii=False), encoding="utf-8")

    conn = db.get_conn(MOCK_DIR / "mock.db")
    db.init_db(conn)
    words = db.load_keywords_from_file(config.KEYWORDS_FILE)
    keywords = db.sync_keywords(conn, words)

    fetcher = MockFetcher(SYNTH)
    s = crawler.crawl_all(conn, fetcher, keywords, "2026-10-09")
    print(f"采集: ok={s.keywords_ok}/{s.keywords_total} items={s.items_saved} errors={s.errors}")

    metrics.build_daily_stats(conn, "2026-10-09")
    scored, zeros, meta = metrics.compute_scores(conn, "2026-10-09")
    metrics.write_outputs(conn, scored, zeros, meta, "2026-10-09", MOCK_DIR,
                          run_status="success", kw_ok=s.keywords_ok,
                          kw_total=s.keywords_total, items_saved=s.items_saved)

    print("--- hot_keywords.txt ---")
    print((MOCK_DIR / "hot_keywords.txt").read_text(encoding="utf-8"))
    print("--- report ---")
    print((MOCK_DIR / "report_2026-10-09.md").read_text(encoding="utf-8"))

    # 幂等验证：同日重跑，快照行数不变
    s2 = crawler.crawl_all(conn, MockFetcher(SYNTH), keywords, "2026-10-09")
    n = conn.execute("SELECT COUNT(*) FROM item_snapshots").fetchone()[0]
    print(f"重跑后快照总数(应不变): {n}, 本轮写入 {s2.items_saved}")


if __name__ == "__main__":
    main()
