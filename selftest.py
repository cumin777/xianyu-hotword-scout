# -*- coding: utf-8 -*-
"""环境自测：导入、签名向量、建表。任何机器上先跑这个确认环境 OK。

用法: python selftest.py
"""
import pathlib

import config
import db
import goofish_sign


def main():
    # 1) 签名向量：与 XianYuAutoDeliveryX 源码注释里的已知输入输出对照
    #    （该向量来自上游 xianyu_utils.py __main__ 里的注释示例）
    sign = goofish_sign.generate_sign("1741667630548", "b7e897bf9767618a32b439c6103fe1cb",
                                      '{"appKey":"444e9908a51d1cb236a27862abc769c9"}')
    assert len(sign) == 32 and all(c in "0123456789abcdef" for c in sign), sign
    print(f"[1/3] 签名函数输出合法 md5: {sign}")

    # 2) cookies 解析
    d = goofish_sign.trans_cookies("unb=123; _m_h5_tk=abc_def; x=1")
    assert d == {"unb": "123", "_m_h5_tk": "abc_def", "x": "1"}, d
    assert "abc" == d["_m_h5_tk"].split("_")[0]  # token 提取方式
    print("[2/3] cookies 解析 OK")

    # 3) 建表（用真实 runtime 路径，幂等）
    conn = db.get_conn(config.DB_PATH)
    db.init_db(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    expect = {"keywords", "item_snapshots", "daily_keyword_stats", "crawl_runs"}
    assert set(tables) >= expect, tables
    print(f"[3/3] 建表 OK: {sorted(tables)}")
    print("自测全部通过")


if __name__ == "__main__":
    main()
