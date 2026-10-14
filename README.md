# 闲鱼热词侦察 xianyu-hotword-scout

按关键词每日采集闲鱼在售商品数据，自动计算 **供需比 / 想要数增速 / 价格带 / 竞争度**，产出爆款关键词排行榜。纯 Python、零数据库服务（SQLite 单文件）、除 requests 外零第三方依赖，每天跑一次一次几毛钱请求量。

适合场景：虚拟资料/选品调研——你维护一份种子关键词表，它每天告诉你哪个词背后的市场值得做。

## 协议现状（2026-10 实测，看这个能省你半天）

| 事项 | 状态 |
|---|---|
| 搜索接口 `mtop.taobao.idlemtopsearch.pc.search/1.0` | ✅ 可用，**需登录 cookies**（匿名直调已被 `RGV587_ERROR::SM` 风控拦截，换 UA 无效） |
| **搜索列表的想要数** | ❌ **2026 起已清空**：`clickParam.args.wantNum` 恒 `"0"`、`exContent.want` 恒空串（2024-2025 的老教程/老项目都在吃这个坑） |
| 想要数正确来源 | ✅ 详情接口 `mtop.taobao.idle.pc.detail/1.0` 的 `data.itemDO.wantCnt`，本工具按此实现 |
| 搜索联想 `mtop.taobao.idlemtopsearch.pc.search.suggest/1.0` | ✅ 可用（2026-10 实测，PC 端下拉联想；data: `{inputWords, searchReqFromPage:"xyPcHome", bucketId:30, type:0}`，取 `data.items[].suggest`） |
| 搜索接口 v2（`/2.0/`） | ❌ 不存在（`FAIL_SYS_API_NOT_FOUNDED`） |
| 请求头 | ⚠️ 精简到 4 项即可；多余的 `Sec-Fetch-*` 头会触发风控超时 |
| 签名 | 标准 h5 mtop：`md5(token & t & appKey34839810 & data)`，token 取 cookie `_m_h5_tk` 下划线前段 |

## 效果示例（真实数据，Day 1）

```
| 排名 | 关键词           | 综合分 | 供需比 | 想要总数 | 在售 | 增速 | 价格P25/P50/P75 |
|   1  | PPT模板          | 0.87  | 535.9 | ~32155  | 60  |  —  | 1/1/1  |
|   2  | 考研资料         | 0.72  | 435.7 | ~26142  | 60  |  —  | 1/2/3  |
|   3  | 注册电气工程师资料 | 0.13  |  32.3 |  ~1938  | 60  |  —  | 4/12/36 |
```

第 8 天起增速指标自动启用（需要历史数据），榜单从"当日快照"升级为"趋势判断"。

## 快速开始

```
git clone https://github.com/cumin777/xianyu-hotword-scout.git
cd xianyu-hotword-scout
python selftest.py          # 环境自测（Python 3.10+，无需装任何包）
```

1. **填 cookies**（必需）：浏览器登录闲鱼 www.goofish.com → F12 → 网络 → 随便搜个词 →
   点列表里 `h5api.m.goofish.com` 的请求 → 标头 → 请求标头 → 复制整行 `cookie:` 值 →
   粘贴到 `runtime\cookies.txt`（单行，文件在首次运行后自动建目录，手动建也行）。
   格式详见 `cookies.example.txt`。粘的串里需含 `unb=`、`cookie2=`、`_m_h5_tk=`。
2. **改关键词表**：`keywords.txt`，每行一个，`#` 开头是注释。这就是你的选品范围（滚雪球词池不占这里，自动扩张）。
3. **跑**：`python main.py`
4. **（可选，推荐）装 jieba 升级挖词质量**：`pip install jieba`——词级挖掘（1~3 个连续词 + 词性收尾过滤），字符滑窗的"跨词碎片"（如从"全流程实操资料"里挖出"程实操资"）从根上消失；不装自动回落字符窗，功能不受影响。

产出（默认都在 `runtime\`，可用环境变量整体重定，见下）：
- `hot_keywords.txt` —— 每行一个关键词，按综合分降序。**机器消费格式，保持稳定**，直接给下游脚本读
- `report_日期.md` —— 人看的日报：指标明细表、样本量警告、零在售提醒
- `xianyu.db` —— 全部历史快照（SQLite，可用任意工具查）

## 自定义产物路径

设置环境变量即可把全部产物（db/报告/热词/cookies/日志）整体挪到你指定的目录：

```
set HOTWORD_SCOUT_RUNTIME=D:\data\hotword-scout
```

- `run_daily.bat` 自动认这个变量（日志也跟着走），定时任务里想换产物盘改系统环境变量即可
- 换路径后已有的 `cookies.txt` / `xianyu.db` 记得一起搬过去，否则等于冷启动
- 不设置 = 默认仓库内 `runtime\`，行为与旧版完全一致

## 定时运行（Windows 任务计划）

```
schtasks /Create /TN "xianyu-hotword-scout" /TR "D:\你的路径\xianyu-hotword-scout\run_daily.bat" /SC DAILY /ST 08:30 /F
```

Linux 用 crontab 等价替换即可（脚本本身无 Windows 依赖，bat 只是包装）。

## 滚雪球自动选词（snowball.py，2026-10-13）

只维护**种子锚点词**（keywords.txt），其余交给词池自演化：自动找词 → 试爬验证 → 达标转正 → 弱势淘汰。`config.SNOWBALL_ENABLED=False` 可回到老行为（只爬种子词、全量出榜）。

```
三源候选                     四道门槛                  状态机
① 快照标题 n-gram(主力)  →   词形 2~8字去泛词/去碎片 →  trial 试爬 3 天
② 搜索联想(买家真实词)  →   平台红线黑名单            日均在售≥50 且 供需比≥1 → active
③ 自家品反馈(想要数涨)  →   同市场去重(词形+命中集)   trial 不达标 / active 连续5天弱 → retired
                             热度证据(≥10 商品命中)    雪球 active 池上限 40，满员末位让位
```

- **节奏**：每日新进 trial ≤ 5 词（词数=请求量）；联想接口只对当日榜单前 8 词各调 1 次
- **准入排序**：反馈加权的钱方向 > 更具体的市场短语（密度=想要数/命中数，宽词分数天然更高，按分会选宽弃专）> 联想补位
- **质量三闸（2026-10-14 首日实测迭代）**：① 命中横跨 ≥3 个在爬词 = 跨市场通用语（"自动发货/烂大街的"），弃；② jieba 词级 gram + 单字/数字 token 断链（治"程实操资"式跨词碎片）；③ 词性收尾过滤——市场词必名词性收尾（n/v/a/eng），"不是外面"式话术碎片不进候选；另有卖家承诺话术黑名单（拍下/秒发/后续费用…）
- **出词**：`hot_keywords.txt` 只出 active 池复合分 Top-M（默认 6，trial 不出品），种子词年份旁注照旧回填
- **副产物**：`ngram_top.txt` 当日高频修饰词（宽口径，泛词/真题正是修饰词），喂下游文案标题带搜索长尾
- **反馈闭环**：自家的 publish_log（jsonl：`item_id/title/ts`）与每日快照交叉，想要数上涨的品其标题词加权扩张——雪往钱多处滚。路径按优先级：环境变量 `HOTWORD_SCOUT_PUBLISH_LOG` > config 直配 > `RUNTIME_DIR` 兄弟目录 `publisher\publish_log.jsonl`（配合 xianyu-pipeline 布局免配置）；都没有 = 该源自动关闭
- 种子词（source=seed）不进状态机、不占池上限、永不被机器淘汰；keywords.txt 里删掉的种子才退役
- 所有旋钮集中在 `config.py` 滚雪球段；离线回归 `python test_snowball.py`（挖词/门槛/反馈/状态机/出品全链不出网）

## 指标口径

综合分 = `0.30*供需比 + 0.25*想要总数 + 0.25*7日增速 + 0.20*(1-竞争度)`，各指标当日跨关键词 min-max 归一化，权重在 `config.py` 可调。

- **供需比** = 想要总数 / 在售数（核心信号：需求密度）
- **想要总数** = 防止"3 个商品 9 个想要"的小样本高比值假象
- **7日增速** = 想要总数较 [2,10] 天窗口内最近一天的涨幅；冷启动前 7 天自动降级为三项加权
- **竞争度** = 在售数归一化（在售越多越拥挤，负向计分）
- 想要总数是估算值（`~` 前缀）：每词抽搜索结果前 `DETAIL_SAMPLE_SIZE`（默认 15）条调详情接口取真实想要数，总量 = 均值 × 在售数。想精确就调大，=60 等价全量。

## 常见问题

- **`RGV587_ERROR` / `FAIL_SYS_USER_VALIDATE`**：匿名必死，检查 `runtime\cookies.txt` 是否填了登录态；cookies 失效（隔几天会过期）重新复制一次即可，程序会自动续期 `_m_h5_tk`
- **`TOKEN_FAILED`**：cookie 串里缺 `_m_h5_tk`，说明复制的不是完整请求头
- **想要数全 0**：老项目行为（读搜索列表的 wantNum）；本仓库已改走详情接口
- **某词连续 3 天 0 在售**：日报会标注——要么词太冷，要么被静默风控，人工开浏览器验证

## 免责声明

本项目仅供学习交流与技术研究，请勿用于商业用途。数据来自公开页面，请自觉控制采集频率（默认日级低频 + 随机延时）并遵守平台服务条款。使用本工具产生的一切后果由使用者自行承担。

## 致谢

- 签名实现：[XianYuAutoDeliveryX](https://gitee.com/llj_git/xian-yu-auto-delivery-x)
- 接口路径参考：[goofish-client](https://github.com/11273/goofish-client)、[pyxianyu](https://github.com/DoLovya/pyxianyu)、掘金闲鱼逆向系列文章
- 同类思路（单品监控、Playwright 路线）：[ai-goofish-monitor](https://github.com/usagi-org/ai-goofish-monitor)
