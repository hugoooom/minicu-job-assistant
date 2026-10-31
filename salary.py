"""整理薪资显示文本；只补回原页面明确提供的单位和发薪后缀。"""

import re
import unicodedata


def normalize_salary(observed, reference=""):
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", observed))
    raw = re.sub(r"\s+", "", unicodedata.normalize("NFKC", reference))
    text = re.sub(r"[–—~～至]", "-", text)
    if "面议" in text:
        return {"salary": "面议", "salary_unit": None, "salary_period": None,
                "salary_pay_months": None, "salary_status": "negotiable"}
    numbers = re.match(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", text)
    if not numbers:
        raise ValueError("薪资识别结果没有可用的金额。")
    # 原文单位优先，避免 OCR 漏掉短小的 K、元/天、薪 等后缀。
    unit_match = re.search(r"K|k|万|元", raw) or re.search(r"K|k|万|元", text)
    amount_unit = unit_match.group().upper() if unit_match else None
    period_match = re.search(r"[/／](小时|时|天|日|周|月|年)", raw) or re.search(
        r"[/／](小时|时|天|日|周|月|年)", text
    )
    period = period_match.group(1) if period_match else None
    period = {"日": "天", "时": "小时"}.get(period, period)
    months_match = re.search(r"(\d+)薪", raw) or re.search(r"(\d+)薪", text)
    if not months_match and "薪" in raw:
        months_match = re.search(r"[·•](\d+)$", text)
    months = int(months_match.group(1)) if months_match else None
    # 一份没有任何单位的输入，仍然标为未知，不擅自推断成月薪。
    unit = (amount_unit or "") + (f"/{period}" if period else "")
    value = numbers.group() + unit + (f"·{months}薪" if months else "")
    result = {"salary": value, "salary_unit": unit or None, "salary_period": period,
              "salary_pay_months": months, "salary_status": "ok" if amount_unit else "unit_unknown"}
    if not amount_unit:
        result["salary_warning"] = "页面与识别结果均未提供明确金额单位，未推断。"
    return result
