# -*- coding: utf-8 -*-
"""mtop 搜索响应解析：data.resultList[] -> ItemRecord 列表。

协议变更时唯一要改的文件——结构变了在这调路径，别处不动。
"""
import re
from dataclasses import dataclass, field


@dataclass
class ItemRecord:
    """一条在售商品快照"""
    item_id: str
    title: str
    price: float | None        # 解析不出存 None，聚合时忽略
    want_count: int | None     # 两个来源都取不到存 None
    area: str | None
    seller_nick: str | None
    publish_time: str | None
    pic_url: str | None


@dataclass
class ParseResult:
    items: list = field(default_factory=list)
    skipped: int = 0    # 缺关键字段被跳过的条数
    total: int = 0      # resultList 总条数


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_search_response(resp_json: dict) -> ParseResult:
    """解析一次搜索响应。单条缺字段跳过并计数，不整批失败。"""
    data = resp_json.get("data") or {}
    result_list = data.get("resultList") or []
    items, skipped = [], 0
    for entry in result_list:
        try:
            main = entry["data"]["item"]["main"]
        except (KeyError, TypeError):
            skipped += 1
            continue
        ex = main.get("exContent") or {}
        item_id = str(ex.get("itemId") or _arg(main, "item_id") or _arg(main, "id") or "")
        title = ex.get("title")
        if not item_id or not title:
            skipped += 1
            continue
        items.append(ItemRecord(
            item_id=item_id,
            title=title,
            price=_price(ex),
            want_count=_want(main, ex),
            area=ex.get("area"),
            seller_nick=ex.get("userNickName"),
            publish_time=ex.get("publishTime"),
            pic_url=ex.get("picUrl"),
        ))
    return ParseResult(items=items, skipped=skipped, total=len(result_list))


def _arg(main: dict, key: str):
    """取 main.clickParam.args 里的字段（防御式）"""
    try:
        return main["clickParam"]["args"].get(key)
    except (KeyError, TypeError, AttributeError):
        return None


def _price(ex: dict) -> float | None:
    """price 是 [{'text':'¥'},{'text':'12.9'},...] 数组：拼接全部 text 取第一个数字。

    "10~20" 这类区间价取下界 10——对价格带统计是合理近似。
    """
    parts = ex.get("price") or []
    if isinstance(parts, list):
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
    else:
        text = str(parts)
    m = _NUMBER_RE.search(text)
    return float(m.group()) if m else None


def _want(main: dict, ex: dict) -> int | None:
    """想要数：优先 clickParam.args.wantNum（数字），兜底 exContent.want（"N人想要"）"""
    v = _arg(main, "wantNum")
    if v is None:
        w = ex.get("want")
        if isinstance(w, str):
            m = re.search(r"\d+", w)
            v = m.group() if m else None
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
