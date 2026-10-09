# -*- coding: utf-8 -*-
"""闲鱼 h5 mtop 签名工具。

签名实现源自 XianYuAutoDeliveryX（https://gitee.com/llj_git/xian-yu-auto-delivery-x），
仅保留本项目用到的两个函数，逻辑未改动。
"""
import hashlib

import config


def trans_cookies(cookies_str: str) -> dict:
    """cookies 字符串 'k1=v1; k2=v2' -> dict"""
    cookies = {}
    for cookie in (cookies_str or "").split("; "):
        if "=" in cookie:
            key, value = cookie.split("=", 1)
            cookies[key] = value
    return cookies


def generate_sign(t: str, token: str, data: str) -> str:
    """标准 h5 mtop 签名：md5(token & 时间戳 & appKey & data)

    token 取 cookie _m_h5_tk 下划线前段；匿名（无 cookie）时传空串。
    """
    msg = f"{token}&{t}&{config.APP_KEY}&{data}"
    return hashlib.md5(msg.encode("utf-8")).hexdigest()
