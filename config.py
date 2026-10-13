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

# ---------- 滚雪球自动选词（PLAN #7：自动找词 + 自动挑词，人工只剩锚点词） ----------
# 词池状态机：候选(四道门槛) -> trial 试爬 -> 达标转 active；active 上限，末位淘汰。
# 出词：hot_keywords.txt 只出 active 池复合分 Top-M（原来全量透传）。
SNOWBALL_ENABLED = True             # 总开关：False = 老行为（只爬种子词，全量出榜）
MAX_NEW_WORDS_PER_DAY = 5           # 每日新进 trial 词数上限（词数=请求量，RGV587 教训）
ACTIVE_POOL_CAP = 40                # 雪球 active 词池上限（种子锚点词不计入、不可被淘汰）
TRIAL_DAYS = 3                      # trial 试爬天数（有数据的天数，够了才评估）
TRIAL_MIN_ITEMS = 50                # trial 达标①：日均在售数（有竞争=有市场；2页60条可见）
TRIAL_MIN_RATIO = 1.0               # trial 达标②：日均供需比 ≥1（想要追得上在售）
ACTIVE_BAD_RATIO = 1.0              # active 末位淘汰线：供需比连续 N 天低于此值
ACTIVE_BAD_DAYS = 5                 # 连续 N 天 → 退役

# 词形与黑名单（门槛①②）
CAND_MIN_CHARS, CAND_MAX_CHARS = 2, 8   # 候选词长度（去空白后）
GENERIC_WORDS = ("资料", "真题卷", "真题", "模板", "教程", "大全", "合集", "电子版", "汇总",
                 "整理", "全套", "系列", "资料包", "素材", "课程", "包", "合集包")
                 # 泛词剥掉后没有剩字的候选不收（单"资料/教程"不收），与 publisher copywriter 同源口径
BLACKLIST = ("网课", "破解", "盗版", "外挂", "辅助", "账号", "租号", "借号", "代练", "代刷",
             "刷单", "引流", "网赚", "兼职", "微信", "加v", "威信", "vx", "私聊", "低价出",
             "免费领", "白嫖", "福利", "回收", "出租", "多少钱", "怎么样", "百度网盘", "网盘资源")
             # 平台红线/导流词：候选含任一即弃（substring 匹配）

# 源①：快照标题 n-gram 挖掘（主力，零新增协议）
NGRAM_MIN_LEN, NGRAM_MAX_LEN = 2, 4  # 滑窗长度（字），中文词组主力区间
NGRAM_MIN_HITS = 10                  # n-gram 至少出现在 N 个不同商品标题（热度证据）

# 源②：搜索联想接口（买家下拉真实搜索词，+1 mtop 调用/词）
SUGGEST_API_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search.suggest/1.0/"
SUGGEST_DATA_TEMPLATE = {"inputWords": "", "searchReqFromPage": "xyPcHome", "bucketId": 30, "type": 0}
SUGGEST_FOR_TOP_N = 8                # 只对当日榜单前 N 词调联想（请求预算）
SUGGEST_ENABLED = True               # 接口失败自动降级（n-gram 独撑），不毁主链

# 源③：自家品反馈闭环（publish_log 的 itemId 与每日快照交叉，想要数涨的市场加权扩张）
# 路径优先级：环境变量 HOTWORD_SCOUT_PUBLISH_LOG > RUNTIME_DIR 兄弟目录 publisher\publish_log.jsonl
# （本仓库独立开源，默认找不到文件 = 反馈源关闭，不影响其他源）
FEEDBACK_PUBLISH_LOG = ""
FEEDBACK_BOOST = 3.0                 # 反馈命中的候选分数乘数（雪往钱多处滚）
FEEDBACK_MAX_AGE_DAYS = 60           # 只看发布距今 N 天内的自家品（太老的市场信号过期）

# 出词与联动
TOP_M_OUTPUT = 6                     # hot_keywords.txt 只出 active 复合分 Top-M（给 restock）
NGRAM_TOP_PATH = RUNTIME_DIR / "ngram_top.txt"   # 当日高频修饰词表（喂 copywriter 标题长尾）
NGRAM_TOP_N = 40                     # 修饰词表条数（门槛比候选宽：泛词/真题/冲刺正是要的修饰词）
