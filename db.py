# -*- coding: utf-8 -*-
"""SQLite 存取层：建表 + 全部 SQL 封装。函数式风格，conn 由调用方显式传参。"""
import re
import sqlite3
from pathlib import Path

# 种子词行尾年份旁注（板绘教程@2026）：流水线元数据（restock 选链偏好，PLAN #11），
# 不属于市场搜索词——读取时剥掉，输出 hot_keywords.txt 时再回填。
_YEAR_TAG_RE = re.compile(r"@(20\d{2})\s*$")

DDL = """
-- 种子关键词表（与 keywords.txt 每日同步；滚雪球词由状态机管理，不受 txt 影响）
-- state: trial(试爬) / active(正式) / retired(退役，历史保留)；source: seed(锚点) 或发现来源
CREATE TABLE IF NOT EXISTS keywords (
    keyword_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1,   -- =1 当日采集（state 为 trial/active 的词）
    source      TEXT NOT NULL DEFAULT 'seed',
    state       TEXT NOT NULL DEFAULT 'active',
    state_since TEXT,                         -- 进入当前状态的日期（trial 计天用）
    note        TEXT,                         -- 溯源：候选分/命中数/来源市场
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
    # 滚雪球词池（PLAN #7）：老库 keywords 全是种子词，默认值即正确语义
    kcols = {r[1] for r in conn.execute("PRAGMA table_info(keywords)")}
    for col, ddl in (("source", "TEXT NOT NULL DEFAULT 'seed'"),
                     ("state", "TEXT NOT NULL DEFAULT 'active'"),
                     ("state_since", "TEXT"),
                     ("note", "TEXT")):
        if col not in kcols:
            conn.execute(f"ALTER TABLE keywords ADD COLUMN {col} {ddl}")


def load_keywords_from_file(path: Path) -> list[str]:
    """读种子关键词文件：去空白、去注释、保序去重。

    行尾 `@2026` 年份旁注会先剥掉——裸词才进采集与打分（旁注是给下游
    restock 选链用的元数据，不是市场搜索词）。
    """
    words, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        word = _YEAR_TAG_RE.sub("", line.strip()).strip()
        if not word or word.startswith("#") or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return words


def load_year_prefs(path: Path) -> dict[str, str]:
    """种子文件 -> {裸词: 年份}，供 hot_keywords.txt 输出时回填旁注。文件缺失返回空。"""
    prefs: dict[str, str] = {}
    if not path.exists():
        return prefs
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        m = _YEAR_TAG_RE.search(stripped)
        if not m:
            continue
        word = _YEAR_TAG_RE.sub("", stripped).strip()
        if word and not word.startswith("#"):
            prefs[word] = m.group(1)
    return prefs


def sync_keywords(conn: sqlite3.Connection, words: list[str]) -> dict[str, int]:
    """keywords.txt（种子锚点词）-> keywords 表。返回 {种子词: keyword_id}。

    只管理 source='seed' 的行：新种子插入；txt 里删掉的种子退役（retired，历史保留）。
    滚雪球词（source!=seed）不归 txt 管，由 snowball 状态机独立维护。
    """
    for word in words:
        conn.execute(
            "INSERT INTO keywords(keyword, source, state, state_since, active) "
            "VALUES(?, 'seed', 'active', date('now', 'localtime'), 1) "
            "ON CONFLICT(keyword) DO UPDATE SET active=1, state='active', source='seed'",
            (word,),
        )
    if words:
        conn.execute(
            "UPDATE keywords SET active=0, state='retired', "
            "state_since=date('now', 'localtime') "
            "WHERE source='seed' AND keyword NOT IN (%s)" % ",".join("?" * len(words)),
            tuple(words),
        )
    conn.commit()
    rows = conn.execute("SELECT keyword_id, keyword FROM keywords WHERE source='seed'").fetchall()
    return {r["keyword"]: r["keyword_id"] for r in rows}


# ---------- 滚雪球词池（PLAN #7） ----------

def get_crawl_words(conn: sqlite3.Connection) -> dict[str, int]:
    """当日要爬的词 = 全部 active + trial（种子词 state 恒为 active，天然包含）。"""
    rows = conn.execute(
        "SELECT keyword_id, keyword FROM keywords WHERE state IN ('active', 'trial')"
    ).fetchall()
    return {r["keyword"]: r["keyword_id"] for r in rows}


def get_all_words(conn: sqlite3.Connection) -> list:
    """词池全景（含 retired——候选同市场去重要对全部历史词做，防退役词反复回炉）。"""
    return conn.execute(
        "SELECT keyword_id, keyword, source, state, state_since, note FROM keywords"
    ).fetchall()


def get_snowball_actives(conn: sqlite3.Connection) -> list:
    """雪球 active 词（不含种子——种子不占池、不可被末位淘汰）。"""
    return conn.execute(
        "SELECT keyword_id, keyword, state_since FROM keywords "
        "WHERE state='active' AND source != 'seed'"
    ).fetchall()


def add_snowball_words(conn: sqlite3.Connection, entries: list[tuple[str, str, str]],
                       run_date: str) -> list[str]:
    """新词进 trial。entries = [(word, source, note)]。返回实际插入的词（重名跳过）。"""
    inserted = []
    for word, source, note in entries:
        cur = conn.execute(
            "INSERT OR IGNORE INTO keywords(keyword, source, state, state_since, note, active) "
            "VALUES(?, ?, 'trial', ?, ?, 1)",
            (word, source, run_date, note),
        )
        if cur.rowcount > 0:
            inserted.append(word)
    conn.commit()
    return inserted


def set_word_state(conn: sqlite3.Connection, kw_id: int, state: str,
                   run_date: str, note: str | None = None) -> None:
    """状态机迁移：state/state_since/active 一起翻（retired 即停采）。note=None 保留原值。"""
    conn.execute(
        "UPDATE keywords SET state=?, state_since=?, active=?, "
        + ("note=?" if note is not None else "note=note") + " WHERE keyword_id=?",
        (state, run_date, 1 if state in ("active", "trial") else 0,
         *([] if note is None else [note]), kw_id),
    )
    conn.commit()


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
    """当日全部关键词的聚合行（打分用），附带关键词文本与词池状态（出榜过滤用）"""
    return conn.execute(
        "SELECT s.*, k.keyword, k.source, k.state, k.state_since FROM daily_keyword_stats s "
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
    """某词最近 N 天聚合行（含今天），按日期倒序——算连续 0 在售/连续弱势天数用"""
    return conn.execute(
        "SELECT crawl_date, item_count, want_total FROM daily_keyword_stats "
        "WHERE keyword_id=? ORDER BY crawl_date DESC LIMIT ?",
        (keyword_id, days),
    ).fetchall()


def get_stats_since(conn: sqlite3.Connection, keyword_id: int, since: str) -> list:
    """某词自 since（含）以来的聚合行，按日期正序——trial 评估用（state_since 起）。"""
    return conn.execute(
        "SELECT crawl_date, item_count, want_total FROM daily_keyword_stats "
        "WHERE keyword_id=? AND crawl_date>=? ORDER BY crawl_date",
        (keyword_id, since),
    ).fetchall()


# ---------- 滚雪球挖词/反馈用的快照查询 ----------

def get_titles_for_date(conn: sqlite3.Connection, run_date: str) -> list:
    """当日全部快照的 (item_id, title, want_count)——n-gram 挖词原料。"""
    return conn.execute(
        "SELECT item_id, title, want_count FROM item_snapshots WHERE crawl_date=?",
        (run_date,),
    ).fetchall()


def get_item_wants(conn: sqlite3.Connection, item_id: str) -> list[tuple]:
    """某商品 (crawl_date, want_count) 全历史，日期倒序——自家品想要数走势。"""
    return conn.execute(
        "SELECT crawl_date, want_count FROM item_snapshots "
        "WHERE item_id=? ORDER BY crawl_date DESC",
        (item_id,),
    ).fetchall()


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
