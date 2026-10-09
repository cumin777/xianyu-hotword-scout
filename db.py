# -*- coding: utf-8 -*-
"""SQLite 存取层：建表 + 全部 SQL 封装。函数式风格，conn 由调用方显式传参。"""
import sqlite3
from pathlib import Path

DDL = """
-- 种子关键词表（与 keywords.txt 每日同步；txt 里删掉的词置 active=0，历史保留）
CREATE TABLE IF NOT EXISTS keywords (
    keyword_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 每日商品快照：一天 x 一词 x 一商品 = 一行。want 历史天然隐含在这张表里
CREATE TABLE IF NOT EXISTS item_snapshots (
    crawl_date   TEXT NOT NULL,               -- '2026-10-09'
    keyword_id   INTEGER NOT NULL,
    item_id      TEXT NOT NULL,               -- 闲鱼商品ID是长数字串，必须 TEXT 防 precision 丢失
    title        TEXT,
    price        REAL,                        -- 解析不出存 NULL，聚合时忽略
    want_count   INTEGER,                     -- 两个来源都取不到存 NULL
    area         TEXT,
    seller_nick  TEXT,
    publish_time TEXT,
    pic_url      TEXT,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (crawl_date, keyword_id, item_id)   -- 当天重跑 OR REPLACE，幂等
);
CREATE INDEX IF NOT EXISTS idx_snap_item    ON item_snapshots(item_id, crawl_date);
CREATE INDEX IF NOT EXISTS idx_snap_kw_date ON item_snapshots(keyword_id, crawl_date);

-- 关键词日聚合：增速指标只查这张小表（每天 = 关键词数 行）
CREATE TABLE IF NOT EXISTS daily_keyword_stats (
    crawl_date   TEXT NOT NULL,
    keyword_id   INTEGER NOT NULL,
    item_count   INTEGER NOT NULL,            -- 去重 item_id 后的在售数
    want_total   INTEGER NOT NULL DEFAULT 0,  -- 想要总数（抽样时 = 均值x在售数 估算值）
    seller_count INTEGER,                     -- 昵称去重，近似值（仅参考列，不进分数）
    price_p25    REAL,
    price_p50    REAL,
    price_p75    REAL,
    want_sampled_n   INTEGER,                 -- 实际采到想要数的商品数（0=采样失败）
    want_sampled_avg REAL,                    -- 采样均值
    PRIMARY KEY (crawl_date, keyword_id)
);

-- 运行日志：每次执行一行
CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running/success/partial/failed/captcha
    keywords_total  INTEGER NOT NULL DEFAULT 0,
    keywords_ok     INTEGER NOT NULL DEFAULT 0,
    keywords_failed INTEGER NOT NULL DEFAULT 0,
    items_saved     INTEGER NOT NULL DEFAULT 0,
    error_summary   TEXT                      -- 每条一行："关键词: 错误描述"
);
"""


def get_conn(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """老库平滑升级：补齐 DDL 里后来加的列（已有新列的库无副作用）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_keyword_stats)")}
    if "want_sampled_n" not in cols:
        conn.execute("ALTER TABLE daily_keyword_stats ADD COLUMN want_sampled_n INTEGER")
        conn.execute("ALTER TABLE daily_keyword_stats ADD COLUMN want_sampled_avg REAL")


def load_keywords_from_file(path: Path) -> list[str]:
    """读种子关键词文件：去空白、去注释、保序去重"""
    words, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        if not word or word.startswith("#") or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return words


def sync_keywords(conn: sqlite3.Connection, words: list[str]) -> dict[str, int]:
    """keywords.txt -> keywords 表。返回 {关键词: keyword_id}。

    新词插入；表里有但 txt 里没有的置 active=0（历史数据保留，不再采集）。
    """
    for word in words:
        conn.execute(
            "INSERT INTO keywords(keyword) VALUES(?) "
            "ON CONFLICT(keyword) DO UPDATE SET active=1",
            (word,),
        )
    txt_set = set(words)
    if txt_set:
        conn.execute("UPDATE keywords SET active=0 WHERE keyword NOT IN (%s)"
                     % ",".join("?" * len(txt_set)), tuple(txt_set))
    else:
        conn.execute("UPDATE keywords SET active=0")   # txt 空了：全部停采（一般不会发生）
    conn.commit()
    rows = conn.execute("SELECT keyword_id, keyword FROM keywords").fetchall()
    return {r["keyword"]: r["keyword_id"] for r in rows}


def get_active_keywords(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT keyword_id, keyword FROM keywords WHERE active=1").fetchall()
    return {r["keyword"]: r["keyword_id"] for r in rows}


def start_run(conn: sqlite3.Connection, run_date: str) -> int:
    cur = conn.execute(
        "INSERT INTO crawl_runs(run_date, started_at, status) VALUES(?,?, 'running')",
        (run_date, _now()),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str,
               keywords_total: int, keywords_ok: int, keywords_failed: int,
               items_saved: int, error_summary: str) -> None:
    conn.execute(
        "UPDATE crawl_runs SET finished_at=?, status=?, keywords_total=?, keywords_ok=?, "
        "keywords_failed=?, items_saved=?, error_summary=? WHERE run_id=?",
        (_now(), status, keywords_total, keywords_ok, keywords_failed,
         items_saved, error_summary, run_id),
    )
    conn.commit()


def replace_snapshots(conn: sqlite3.Connection, run_date: str, keyword_id: int,
                      items: list) -> int:
    """当日该词快照整体替换（先删后插，单事务）。

    注意不能只用 INSERT OR REPLACE：重跑时搜索排名漂移会带来新的 item_id，
    旧 item 的行会残留，导致当日 in 售数虚高。删旧插新才是真正的幂等。
    """
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM item_snapshots WHERE crawl_date=? AND keyword_id=?",
                    (run_date, keyword_id))
        cur.executemany(
            "INSERT INTO item_snapshots"
            "(crawl_date, keyword_id, item_id, title, price, want_count, area, "
            " seller_nick, publish_time, pic_url) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(run_date, keyword_id, it.item_id, it.title, it.price, it.want_count,
              it.area, it.seller_nick, it.publish_time, it.pic_url) for it in items],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(items)


def get_snapshot_rows(conn: sqlite3.Connection, run_date: str, keyword_id: int) -> list:
    """当日某词全部快照（聚合用）：返回 sqlite3.Row 列表"""
    return conn.execute(
        "SELECT item_id, price, want_count, seller_nick FROM item_snapshots "
        "WHERE crawl_date=? AND keyword_id=?",
        (run_date, keyword_id),
    ).fetchall()


def upsert_stats(conn: sqlite3.Connection, run_date: str, keyword_id: int,
                 stats: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daily_keyword_stats"
        "(crawl_date, keyword_id, item_count, want_total, seller_count, "
        " price_p25, price_p50, price_p75, want_sampled_n, want_sampled_avg) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_date, keyword_id, stats["item_count"], stats["want_total"],
         stats["seller_count"], stats["price_p25"], stats["price_p50"],
         stats["price_p75"], stats["want_sampled_n"], stats["want_sampled_avg"]),
    )
    conn.commit()


def get_stats_rows(conn: sqlite3.Connection, run_date: str) -> list:
    """当日全部关键词的聚合行（打分用），附带关键词文本"""
    return conn.execute(
        "SELECT s.*, k.keyword FROM daily_keyword_stats s "
        "JOIN keywords k USING(keyword_id) WHERE s.crawl_date=?",
        (run_date,),
    ).fetchall()


def get_growth_bases(conn: sqlite3.Connection, today: str) -> dict[int, tuple]:
    """增速基期：[today-10, today-2] 窗口内每个关键词最近一个有数据的日子。

    返回 {keyword_id: (基期日期, 基期 want_total)}
    """
    rows = conn.execute(
        "SELECT s.keyword_id, s.crawl_date, s.want_total FROM daily_keyword_stats s "
        "JOIN (SELECT keyword_id, MAX(crawl_date) AS d FROM daily_keyword_stats "
        "      WHERE crawl_date BETWEEN date(?, '-10 day') AND date(?, '-2 day') "
        "      GROUP BY keyword_id) m "
        "  ON m.keyword_id = s.keyword_id AND m.d = s.crawl_date",
        (today, today),
    ).fetchall()
    return {r["keyword_id"]: (r["crawl_date"], r["want_total"]) for r in rows}


def get_collected_days(conn: sqlite3.Connection) -> int:
    """已积累的采集天数（冷启动进度用）"""
    row = conn.execute("SELECT COUNT(DISTINCT crawl_date) AS n FROM daily_keyword_stats").fetchone()
    return row["n"] if row else 0


def get_first_collected_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MIN(crawl_date) AS d FROM daily_keyword_stats").fetchone()
    return row["d"] if row else None


def get_recent_stats(conn: sqlite3.Connection, keyword_id: int, days: int = 7) -> list:
    """某词最近 N 天聚合行（含今天），按日期倒序——算连续 0 在售天数用"""
    return conn.execute(
        "SELECT crawl_date, item_count FROM daily_keyword_stats "
        "WHERE keyword_id=? ORDER BY crawl_date DESC LIMIT ?",
        (keyword_id, days),
    ).fetchall()


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
