"""BOSS 城市与整区搜索参数；区是搜索范围，不代替岗位实际地址。"""

from urllib.parse import urlencode

CITY_CODES = {"广州": "101280100", "深圳": "101280600"}
DISTRICTS = {
    "广州": {"荔湾区": "440103", "白云区": "440111", "天河区": "440106",
           "越秀区": "440104", "海珠区": "440105", "增城区": "440118",
           "从化区": "440117", "花都区": "440114", "南沙区": "440115",
           "黄埔区": "440112", "番禺区": "440113"},
    "深圳": {"罗湖区": "440303", "坪山区": "440310", "光明区": "440311",
           "盐田区": "440308", "龙华区": "440309", "宝安区": "440306",
           "龙岗区": "440307", "福田区": "440304", "南山区": "440305"},
}


def search_url(city, keyword, district=None):
    params = {"city": CITY_CODES[city], "query": keyword}
    if district is not None:
        params["multiBusinessDistrict"] = DISTRICTS[city][district]
    return "https://www.zhipin.com/web/geek/jobs?" + urlencode(params)


def selected_districts(city, names=None):
    names = list(dict.fromkeys(names if names is not None else DISTRICTS[city]))
    unknown = [name for name in names if name not in DISTRICTS[city]]
    if unknown:
        raise ValueError(f"{city} 不支持这些区：{'、'.join(unknown)}；可选：{'、'.join(DISTRICTS[city])}")
    return names
