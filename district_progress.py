"""记录同一城市、关键词和分区范围的续采位置，保存在被 Git 忽略的 output 中。"""

import hashlib
import json
from datetime import datetime

from job_history import normalized_name


class DistrictProgress:
    def __init__(self, path, city, keyword, districts):
        self.path = path
        scope = json.dumps([city, normalized_name(keyword), districts], ensure_ascii=False)
        self.key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        self.data = {"version": 1, "searches": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict) or self.data.get("version") != 1 or not isinstance(self.data.get("searches"), dict):
                raise ValueError(f"分区进度文件格式不正确：{path}")
            if any(not isinstance(value, dict) or not isinstance(value.get("district"), str)
                   for value in self.data["searches"].values()):
                raise ValueError(f"分区进度记录格式不正确：{path}")
        self.city, self.keyword = city, keyword

    def order(self, districts, restart=False):
        last = self.data["searches"].get(self.key, {}).get("district")
        if restart or last not in districts:
            return list(districts)
        index = districts.index(last)
        return list(districts[index:]) + list(districts[:index])

    def save(self, district):
        self.data["searches"][self.key] = {"city": self.city, "keyword": self.keyword,
            "district": district, "updated_at": datetime.now().isoformat(timespec="seconds")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
