"""内存库隐私脱敏：身份类信息在**入库前**与**输出前**统一替换。

规则（2026-10-19 用户口径）：身份类信息一律隐藏、禁止使用/引用/外发。
保留长度与可读性，值替换为 <已隐藏:类型>。
"""
from __future__ import annotations

import re

# 身份证：18 位，出生日期段合法（不校验省份/校验位，宁可多遮）
ID_RE = re.compile(r"(?<![0-9A-Za-z])(\d{6})((?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(\d{3}[\dXx])(?![0-9A-Za-z])")
PHONE_RE = re.compile(r"(?<![0-9A-Za-z])(1[3-9]\d{9})(?![0-9A-Za-z])")
SOCIAL_RE = re.compile(r"((?:微信号|微信|QQ号|QQ\s*[:：]|支付宝|抖音号|小红书号)\s*[:：]?\s*)([A-Za-z0-9_\-]{4,30})")
NAME_RE = re.compile(r"((?:姓名|真实姓名)\s*[:：]\s*)([\u4e00-\u9fa5]{2,4})")
BIRTH_RE = re.compile(r"((?:出生日期|生日)\s*[:：]?\s*)(\d{4}[-年/]\d{1,2}[-月/]\d{1,2})")
ADDR_RE = re.compile(r"((?:家庭住址|家庭地址|住址|收货地址)\s*[:：]?\s*)([^\n，。；]{6,60})")

NAME_STOP = ("项目", "姓名", "日期", "备注", "单位", "电话", "手机", "身份", "性别", "年龄",
             "邮箱", "地址", "编号", "学校", "专业", "班级", "学号", "联系", "信息", "资料",
             "本人", "学生", "老师", "家长", "签章", "签字", "填表", "申请", "审核")


def _mask_name(m: re.Match) -> str:
    head, val = m.group(1), m.group(2)
    if any(w in val for w in NAME_STOP):
        return m.group(0)
    tail = m.string[m.end():m.end() + 1]
    if tail in ("X", "x", "/", "、", "：", ":", ".", "(", "（"):
        return m.group(0)
    return head + "<已隐藏:姓名>"


def _mask_addr(m: re.Match) -> str:
    head, val = m.group(1), m.group(2)
    if "<已隐藏" in val:
        return m.group(0)
    if any(w in val for w in ("隐私", "资料", "信息", "照片", "行踪", "生物特征", "等个人")):
        return m.group(0)
    region = ("省", "市", "区", "县", "镇", "乡", "村")
    place = ("路", "街", "道", "巷", "号", "栋", "幢", "单元", "室", "楼")
    if not (any(k in val for k in region) and any(k in val for k in place)):
        return m.group(0)
    return head + "<已隐藏:住址>"


def redact(text: str) -> str:
    if not text:
        return text
    out = ID_RE.sub("<已隐藏:身份证>", text)
    out = PHONE_RE.sub("<已隐藏:手机号>", out)
    out = SOCIAL_RE.sub(lambda m: m.group(1) + "<已隐藏:社交账号>", out)
    out = NAME_RE.sub(_mask_name, out)
    out = BIRTH_RE.sub(lambda m: m.group(1) + "<已隐藏:生日>", out)
    out = ADDR_RE.sub(_mask_addr, out)
    return out


def has_identity(text: str) -> bool:
    if not text:
        return False
    return redact(text) != text
