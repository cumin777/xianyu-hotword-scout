# -*- coding: utf-8 -*-
"""抓取器。BaseFetcher 抽象 + HttpFetcher（纯 HTTP 直调 mtop 搜索接口）实现。

将来直调路线失效（连续滑块/结构大改）时，新增一个 PlaywrightFetcher
（浏览器打开搜索页、拦截 mtop 响应）替换即可——解析/入库/指标层不动。

命令行单独测试: python fetcher.py 考研资料
"""
import abc
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

import config
import goofish_sign
import parser as parser_mod

# 官方错误码里 EXPIRED 拼成了 EXOIRED，两种都匹配
_TOKEN_ERRORS = ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EMPTY")


class CaptchaError(Exception):
    """命中滑块/风控。抓取器抛出，主流程据此熔断当日采集（不重试不硬闯）。"""


@dataclass
class FetchResult:
    ok: bool                          # True = 请求成功（EMPTY 也算成功，只是没结果）
    items: list = field(default_factory=list)   # ItemRecord 列表
    error: str | None = None          # EMPTY / NETWORK / TOKEN_FAILED / PARSE / API_xxx
    raw: str | None = None            # 最近一次响应原文（成功时保留全文供 sample 落盘）


class BaseFetcher(abc.ABC):
    @abc.abstractmethod
    def search(self, keyword: str, page: int) -> FetchResult: ...

    @abc.abstractmethod
    def get_want(self, item_id: str) -> int | None:
        """单个商品想要数（走详情接口）。失败返回 None；风控抛 CaptchaError。"""

    def suggest(self, keyword: str) -> list[str]:
        """搜索联想词（买家下拉真实搜索词，滚雪球源②）。默认不支持=空。"""
        return []


class HttpFetcher(BaseFetcher):
    def __init__(self, cookie_path: Path):
        self.session = requests.Session()
        self.session.headers.update(config.HTTP_HEADERS)
        if cookie_path.exists():
            raw = cookie_path.read_text(encoding="utf-8").strip()
            for k, v in goofish_sign.trans_cookies(raw).items():
                self.session.cookies.set(k, v, domain=".goofish.com")

    def search(self, keyword: str, page: int) -> FetchResult:
        data = dict(config.SEARCH_DATA_TEMPLATE)
        data["keyword"] = keyword
        data["pageNumber"] = page
        data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

        token_attempts = config.TOKEN_RETRY + 1
        for attempt in range(token_attempts):
            resp, err = self._mtop_request("mtop.taobao.idlemtopsearch.pc.search",
                                           config.SEARCH_API_URL, data_str,
                                           "a21ybx.search.0.0")
            if err == "NETWORK":
                return FetchResult(False, error="NETWORK")
            raw = resp.text
            try:
                body = resp.json()
            except ValueError:
                return FetchResult(False, error="PARSE", raw=raw[:2000])
            ret = ";".join(body.get("ret") or [])

            if any(e in ret for e in _TOKEN_ERRORS):
                if attempt + 1 < token_attempts:
                    time.sleep(1)
                    continue   # session 已收下 Set-Cookie 新 _m_h5_tk，下轮用新 token 重算签名
                return FetchResult(False, error="TOKEN_FAILED", raw=raw[:2000])

            if ("FAIL_SYS_USER_VALIDATE" in ret or "rgv587" in raw.lower()
                    or "deny_h5" in raw.lower()):
                raise CaptchaError(f"{keyword} 第{page}页 命中风控: {ret or 'rgv587/deny_h5'}")

            if not ret.startswith("SUCCESS"):
                return FetchResult(False, error=f"API_{ret[:50]}", raw=raw[:2000])

            parsed = parser_mod.parse_search_response(body)
            if parsed.total > 0 and len(parsed.items) * 2 < parsed.total:
                # 超半数条目解析不出 → 疑似响应结构变更，别把残缺数据当真
                return FetchResult(False, error="PARSE", raw=raw[:2000])
            if not parsed.items:
                return FetchResult(True, items=[], error="EMPTY", raw=raw)
            return FetchResult(True, items=parsed.items, raw=raw)

        return FetchResult(False, error="TOKEN_FAILED")

    def get_want(self, item_id: str) -> int | None:
        """调详情接口拿单个商品想要数（data.itemDO.wantCnt）。

        失败（网络/结构/接口错误）返回 None，不抛——想要数是抽样补数，
        单条失败不该毁掉整轮采集；风控例外（CaptchaError 上抛触发熔断）。
        """
        data = dict(config.DETAIL_DATA_TEMPLATE)
        data["itemId"] = item_id
        data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        token_attempts = config.TOKEN_RETRY + 1
        for attempt in range(token_attempts):
            resp, err = self._mtop_request("mtop.taobao.idle.pc.detail",
                                           config.DETAIL_API_URL, data_str,
                                           "a21ybx.item.detail.0.0")
            if err == "NETWORK":
                return None
            try:
                body = resp.json()
            except ValueError:
                return None
            ret = ";".join(body.get("ret") or [])
            if any(e in ret for e in _TOKEN_ERRORS):
                if attempt + 1 < token_attempts:
                    time.sleep(1)
                    continue
                return None
            if "FAIL_SYS_USER_VALIDATE" in ret or "rgv587" in resp.text.lower() \
                    or "deny_h5" in resp.text.lower():
                raise CaptchaError(f"详情 {item_id} 命中风控: {ret[:60]}")
            if not ret.startswith("SUCCESS"):
                return None
            try:
                return int(body["data"]["itemDO"]["wantCnt"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def suggest(self, keyword: str) -> list[str]:
        """搜索联想接口（滚雪球源②）：data.items[].suggest = 买家下拉真实搜索词。

        调用形状取自 goofish.com PC 端 p_layout.js（2026-10 实测 SUCCESS）。
        失败返回 []（联想是候选来源之一，挂了不该毁采集）；风控抛 CaptchaError。
        """
        data = dict(config.SUGGEST_DATA_TEMPLATE)
        data["inputWords"] = keyword
        data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        token_attempts = config.TOKEN_RETRY + 1
        for attempt in range(token_attempts):
            resp, err = self._mtop_request("mtop.taobao.idlemtopsearch.pc.search.suggest",
                                           config.SUGGEST_API_URL, data_str,
                                           "a21ybx.search.0.0")
            if err == "NETWORK":
                return []
            try:
                body = resp.json()
            except ValueError:
                return []
            ret = ";".join(body.get("ret") or [])
            if any(e in ret for e in _TOKEN_ERRORS):
                if attempt + 1 < token_attempts:
                    time.sleep(1)
                    continue
                return []
            if "FAIL_SYS_USER_VALIDATE" in ret or "rgv587" in resp.text.lower() \
                    or "deny_h5" in resp.text.lower():
                raise CaptchaError(f"联想 {keyword} 命中风控: {ret[:60]}")
            if not ret.startswith("SUCCESS"):
                return []
            try:
                items = body["data"]["items"]
            except (KeyError, TypeError):
                return []
            return [it["suggest"] for it in items
                    if isinstance(it, dict) and it.get("suggest")]
        return []

    def _mtop_request(self, api: str, url: str, data_str: str, spm_cnt: str):
        """发一次签名 mtop 请求（搜索/详情共用）。网络错误重试 NETWORK_RETRY 次。"""
        for i in range(config.NETWORK_RETRY + 1):
            t = str(int(time.time() * 1000))
            sign = goofish_sign.generate_sign(t, self._token(), data_str)
            params = {
                "jsv": "2.7.2", "appKey": config.APP_KEY, "t": t, "sign": sign,
                "v": url.rstrip("/").split("/")[-1], "type": "originaljson",
                "accountSite": "xianyu", "dataType": "json", "timeout": "20000",
                "api": api, "sessionOption": "AutoLoginOnly", "spm_cnt": spm_cnt,
            }
            try:
                resp = self.session.post(url, params=params,
                                         data={"data": data_str},
                                         headers=config.HTTP_HEADERS,
                                         timeout=config.REQUEST_TIMEOUT)
                return resp, None
            except requests.RequestException:
                if i >= config.NETWORK_RETRY:
                    return None, "NETWORK"
                time.sleep(config.NETWORK_RETRY_WAIT)
        return None, "NETWORK"

    def _token(self) -> str:
        """从 session cookie 里取 _m_h5_tk 下划线前段（匿名首跑为空串，属正常）"""
        for c in self.session.cookies:
            if c.name == "_m_h5_tk":
                return c.value.split("_")[0]
        return ""

    def save_cookies(self, cookie_path: Path) -> None:
        """把 session 里最新的 cookies 写回文件（匿名时是 _m_h5_tk 系列，同样值得续用）"""
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        raw = "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)
        cookie_path.write_text(raw, encoding="utf-8")


class MockFetcher(BaseFetcher):
    """离线回归：读 sample_search.json 走解析全链路，不出网。page>=2 返回 EMPTY。"""
    def __init__(self, sample_path: Path):
        self._body = json.loads(sample_path.read_text(encoding="utf-8"))

    def search(self, keyword: str, page: int) -> FetchResult:
        if page > 1:
            return FetchResult(True, items=[], error="EMPTY")
        parsed = parser_mod.parse_search_response(self._body)
        return FetchResult(True, items=parsed.items)

    def get_want(self, item_id: str) -> int | None:
        return 100   # 固定值：回归只验证链路，不验证数值

    def suggest(self, keyword: str) -> list[str]:
        return ["剪辑教程合集", "剪辑素材包"]   # 固定联想词：离线走通源②链路


if __name__ == "__main__":
    # 单词单页连通性测试：python fetcher.py 考研资料
    kw = sys.argv[1] if len(sys.argv) > 1 else "考研资料"
    f = HttpFetcher(config.COOKIE_PATH)
    r = f.search(kw, 1)
    print(f"ok={r.ok} error={r.error} items={len(r.items)}")
    for it in r.items[:5]:
        print(f"  {it.title[:30]!r} price={it.price} want={it.want_count} nick={it.seller_nick}")
