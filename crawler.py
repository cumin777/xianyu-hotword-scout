# -*- coding: utf-8 -*-
"""采集主循环：关键词 x 页 -> 快照入库，过程计数汇总给 crawl_runs。"""
import random
import time
from dataclasses import dataclass, field

import config
import db
from fetcher import BaseFetcher, CaptchaError


@dataclass
class CrawlSummary:
    keywords_total: int = 0
    keywords_ok: int = 0
    keywords_failed: int = 0
    items_saved: int = 0
    errors: list = field(default_factory=list)   # 每条 "关键词: 错误"
    captcha: bool = False                        # True = 触发滑块熔断
    first_raw: str | None = None                 # 第一次成功响应原文（供 sample 落盘）


def crawl_all(conn, fetcher: BaseFetcher, keywords: dict[str, int], run_date: str) -> CrawlSummary:
    """跑完全部关键词。返回汇总；命中滑块立即熔断返回（已采数据主流程照常聚合）。"""
    s = CrawlSummary(keywords_total=len(keywords))
    for word, kw_id in keywords.items():
        n = _crawl_keyword(conn, fetcher, word, kw_id, run_date, s)
        if n is None:
            s.keywords_failed += 1
        else:
            s.keywords_ok += 1
            s.items_saved += n
        time.sleep(random.uniform(config.SLEEP_MIN, config.SLEEP_MAX))   # 词间限流
    return s


def _crawl_keyword(conn, fetcher: BaseFetcher, word: str, kw_id: int,
                   run_date: str, s: CrawlSummary) -> int | None:
    """抓一个关键词的全部页。返回入库条数；整词失败返回 None（错误已记录进 s.errors）。

    CaptchaError 不在此捕获——让它冒到 crawl_all 触发当日熔断。
    """
    seen, records = set(), []
    for page in range(1, config.MAX_PAGES_PER_KEYWORD + 1):
        r = fetcher.search(word, page)
        if s.first_raw is None and r.raw:
            s.first_raw = r.raw
        if r.error == "EMPTY":
            break                                   # 翻到底，正常结束
        if not r.ok:
            s.errors.append(f"{word}: {r.error}")
            break                                   # 部分页成功也保住，整词失败下面判 records
        for it in r.items:
            if it.item_id not in seen:              # 页间去重
                seen.add(it.item_id)
                records.append(it)
        if page < config.MAX_PAGES_PER_KEYWORD:
            time.sleep(random.uniform(config.SLEEP_MIN, config.SLEEP_MAX))   # 页间限流

    # 抽样调详情接口补想要数（2026 搜索列表已不带 want，见 config.DETAIL_SAMPLE_SIZE）
    for i, rec in enumerate(records[:config.DETAIL_SAMPLE_SIZE]):
        want = fetcher.get_want(rec.item_id)     # CaptchaError 自然上抛触发熔断
        if want is not None:
            rec.want_count = want
        if i + 1 < min(len(records), config.DETAIL_SAMPLE_SIZE):
            time.sleep(random.uniform(config.SLEEP_MIN, config.SLEEP_MAX))   # 详情间限流

    if not records and s.errors and s.errors[-1].startswith(f"{word}:"):
        return None                                 # 一页都没成
    return db.replace_snapshots(conn, run_date, kw_id, records)
