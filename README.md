# 闲鱼热词侦察 xianyu-hotword-scout

按关键词每日采集闲鱼在售商品数据，自动计算 **供需比 / 想要数增速 / 价格带 / 竞争度**，产出爆款关键词排行榜。纯 Python、零数据库服务（SQLite 单文件）、除 requests 外零第三方依赖，每天跑一次一次几毛钱请求量。

适合场景：虚拟资料/选品调研——你维护一份种子关键词表，它每天告诉你哪个词背后的市场值得做。

## 协议现状（2026-10 实测，看这个能省你半天）

| 事项 | 状态 |
|---|---|
| 搜索接口 `mtop.taobao.idlemtopsearch.pc.search/1.0` | ✅ 可用，**需登录 cookies**（匿名直调已被 `RGV587_ERROR::SM` 风控拦截，换 UA 无效） |
| **搜索列表的想要数** | ❌ **2026 起已清空**：`clickParam.args.wantNum` 恒 `"0"`、`exContent.want` 恒空串（2024-2025 的老教程/老项目都在吃这个坑） |
| 想要数正确来源 | ✅ 详情接口 `mtop.taobao.idle.pc.detail/1.0` 的 `data.itemDO.wantCnt`，本工具按此实现 |
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
2. **改关键词表**：`keywords.txt`，每行一个，`#` 开头是注释。这就是你的选品范围。
3. **跑**：`python main.py`

产出（都在 `runtime\`）：
- `hot_keywords.txt` —— 每行一个关键词，按综合分降序。**机器消费格式，保持稳定**，直接给下游脚本读
- `report_日期.md` —— 人看的日报：指标明细表、样本量警告、零在售提醒
- `xianyu.db` —— 全部历史快照（SQLite，可用任意工具查）

## 定时运行（Windows 任务计划）

```
schtasks /Create /TN "xianyu-hotword-scout" /TR "D:\你的路径\xianyu-hotword-scout\run_daily.bat" /SC DAILY /ST 08:30 /F
```

Linux 用 crontab 等价替换即可（脚本本身无 Windows 依赖，bat 只是包装）。

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
