# -*- coding: utf-8 -*-
"""research 模块全局配置：所有常量集中在这，想改行为先看这里。"""
import os
from pathlib import Path

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent            # research\
# 产物目录整体可重定：设置环境变量 HOTWORD_SCOUT_RUNTIME=D:\你的路径 即可
# （db/报告/热词/cookies/日志全部跟过去；定时任务/编排脚本自由选盘，默认仍在仓库内）
_rt_env = os.environ.get("HOTWORD_SCOUT_RUNTIME")
RUNTIME_DIR = Path(_rt_env).resolve() if _rt_env else BASE_DIR / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = RUNTIME_DIR / "xianyu.db"
COOKIE_PATH = RUNTIME_DIR / "cookies.txt"             # 空文件=匿名；上小号时整串替换
SAMPLE_PATH = RUNTIME_DIR / "sample_search.json"      # 首次成功响应样本（--mock 回归用）
HOT_KEYWORDS_PATH = RUNTIME_DIR / "hot_keywords.txt"  # 下游爬虫消费的输出
ALERT_DIR = RUNTIME_DIR / "alerts"
KEYWORDS_FILE = BASE_DIR / "keywords.txt"

# ---------- 接口 ----------
SEARCH_API_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
APP_KEY = "34839810"        # 闲鱼网页版公共 appKey（签名用，与 goofish 网页一致）

# 搜索请求 data 模板（来源：goofish-client / 掘金逆向文交叉验证）
SEARCH_DATA_TEMPLATE = {
    "pageNumber": 1,
    "keyword": "",
    "fromFilter": False,
    "rowsPerPage": 30,
    "sortValue": "",
    "sortField": "",
    "customDistance": "",
    "gps": "",
    "propValueStr": {},
    "customGps": "",
    "searchReqFromPage": "pcSearch",
    "extraFilterValue": "{}",
    "userPositionJson": "{}",
}

# 请求头刻意精简：多余的 Sec-Fetch 头会触发风控超时（52pojie 有案例）
HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
    "Referer": "https://www.goofish.com/",
    "Origin": "https://www.goofish.com",
    "Content-Type": "application/x-www-form-urlencoded",
}

# ---------- 详情采集（想要数） ----------
# 2026-10 实测：搜索列表响应已不带想要数（wantNum 恒 0），需逐个商品调详情接口。
# DETAIL_SAMPLE_SIZE = 每个关键词抽搜索结果前 N 条调详情拿想要数，其余用 均值x在售数 估算。
# 调小=更省请求更稳，调大=更准；=60(每词2页总量) 时等价全量精确。
DETAIL_SAMPLE_SIZE = 15
DETAIL_API_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/"
DETAIL_DATA_TEMPLATE = {"itemId": "", "needFlash": False}

# ---------- 采集节奏（风控红线：日级低频，不加速） ----------
MAX_PAGES_PER_KEYWORD = 2    # 每词抓几页（每页 30 条）
SLEEP_MIN = 2.0              # 相邻请求随机延时下限（秒）
SLEEP_MAX = 5.0              # 上限
REQUEST_TIMEOUT = 30         # 单请求超时（秒）
NETWORK_RETRY = 2            # 网络错误重试次数
NETWORK_RETRY_WAIT = 5       # 网络重试间隔（秒）
TOKEN_RETRY = 1              # token 错误重试次数（Set-Cookie 新值重算签名再试一次）

# ---------- 指标权重（正常期；warm-up 期缺增速项按 metrics.py 逐行再归一化） ----------
WEIGHT_DEMAND_SUPPLY = 0.30  # 供需比 = 想要总数/在售数
WEIGHT_VOLUME = 0.25         # 需求规模 = 想要总数
WEIGHT_GROWTH = 0.25         # 7 日想要数增速
WEIGHT_COMPETITION = 0.20    # 竞争度（负向：在售越多越拥挤）

GROWTH_BASE_WINDOW_DAYS = (2, 10)   # 增速基期：[today-10, today-2] 内最近一个有数据的日子
GROWTH_CLIP = (-1.0, 3.0)           # 增速裁剪区间，防极端值污染归一化
MIN_SAMPLE_SIZE = 10                # 在售数 < 此值 → 报告标"样本少"
ZERO_RESULT_ALERT_DAYS = 3          # 连续 N 天 0 在售 → 报告提醒人工复核
