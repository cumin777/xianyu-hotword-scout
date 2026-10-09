# -*- coding: utf-8 -*-
"""research 选品模块入口。

用法:
  python main.py              # 正常：采集 -> 入库 -> 指标 -> 输出
  python main.py --score-only # 不采集，只重算当日指标与输出（调试/验证增速）
  python main.py --mock       # 离线回归：读 sample_search.json 走全链路（不出网，
                              #   写 runtime\\mock\\，不污染真实库）

退出码: 0 全部成功 / 1 部分失败 / 2 滑块熔断(已落告警文件) / 3 致命错误
"""
import argparse
import datetime
import sys

import config
import crawler
import db
import metrics
from fetcher import HttpFetcher, MockFetcher


def main() -> int:
    ap = argparse.ArgumentParser(description="闲鱼选品采集器")
    ap.add_argument("--mock", action="store_true", help="离线回归模式")
    ap.add_argument("--score-only", action="store_true", help="跳过采集只算分")
    args = ap.parse_args()

    run_date = datetime.date.today().isoformat()
    mock_dir = config.RUNTIME_DIR / "mock"
    db_path = mock_dir / "mock.db" if args.mock else config.DB_PATH

    conn = db.get_conn(db_path)
    db.init_db(conn)

    summary, run_status, exit_code = None, "", 0

    if not args.score_only:
        words = db.load_keywords_from_file(config.KEYWORDS_FILE)
        keywords = db.sync_keywords(conn, words)

        run_id = db.start_run(conn, run_date)
        fetcher = MockFetcher(config.SAMPLE_PATH) if args.mock else HttpFetcher(config.COOKIE_PATH)
        try:
            summary = crawler.crawl_all(conn, fetcher, keywords, run_date)
        except Exception as e:                       # 未预期异常 = 致命
            db.finish_run(conn, run_id, "failed", len(keywords), 0, len(keywords), 0, repr(e))
            print(f"[致命] {e}")
            return 3

        # 首次真实成功响应存样本（协议变更后的 --mock 回归基准）
        if not args.mock and summary.first_raw and not config.SAMPLE_PATH.exists():
            config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            config.SAMPLE_PATH.write_text(summary.first_raw, encoding="utf-8")
            print(f"[样本] 已保存首次成功响应 -> {config.SAMPLE_PATH}")

        if isinstance(fetcher, HttpFetcher):
            fetcher.save_cookies(config.COOKIE_PATH)   # _m_h5_tk 等自动续期

        if summary.captcha:
            run_status, exit_code = "captcha", 2
            _write_alert(run_date, summary.errors)
        elif summary.keywords_failed:
            run_status, exit_code = "partial", 1
        else:
            run_status, exit_code = "success", 0

        db.finish_run(conn, run_id, run_status, summary.keywords_total, summary.keywords_ok,
                      summary.keywords_failed, summary.items_saved,
                      "\n".join(summary.errors))
        for e in summary.errors:
            print(f"[错误] {e}")
        print(f"[采集] {run_status}: {summary.keywords_ok}/{summary.keywords_total} 词, "
              f"{summary.items_saved} 条快照")

    # 指标与输出——采集熔断/部分失败也照常出报告（半量好过没有）
    out_dir = mock_dir if args.mock else config.RUNTIME_DIR
    metrics.build_daily_stats(conn, run_date)
    scored, zeros, meta = metrics.compute_scores(conn, run_date)
    metrics.write_outputs(conn, scored, zeros, meta, run_date, out_dir,
                          run_status=run_status or "(score-only)",
                          kw_ok=summary.keywords_ok if summary else len(scored),
                          kw_total=summary.keywords_total if summary else len(scored) + len(zeros),
                          items_saved=summary.items_saved if summary else 0)
    print(f"[输出] 榜单 {len(scored)} 词 -> {out_dir / 'hot_keywords.txt'}")
    print(f"[输出] 日报 -> {out_dir / f'report_{run_date}.md'}")
    return exit_code


def _write_alert(run_date: str, errors: list[str]) -> None:
    """滑块熔断告警文件（现阶段无人值守的替代通知，将来接告警模块）。"""
    config.ALERT_DIR.mkdir(parents=True, exist_ok=True)
    import datetime as _dt
    content = (f"# 风控告警 {run_date}\n\n"
               f"时间: {_dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
               "当日采集已熔断（命中滑块/风控，不重试不硬闯）。\n"
               "已采数据照常出报告。人工处理：过一段时间再跑，或检查 cookies 是否失效。\n\n"
               "## 错误明细\n" + "\n".join(f"- {e}" for e in errors) + "\n")
    (config.ALERT_DIR / f"alert_{run_date}.md").write_text(content, encoding="utf-8")
    print(f"[告警] 已写入 {config.ALERT_DIR / f'alert_{run_date}.md'}")


if __name__ == "__main__":
    sys.exit(main())
