"""将保存的真实岗位卡片和薪资截图组合成一条 JSON；不访问网站。"""

import json
import unicodedata
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

from rapidocr import RapidOCR
from salary import normalize_salary


OUTPUT = Path(__file__).resolve().parent / "output"
FIELD_CLASSES = {
    "job-name": "title",
    "boss-name": "company",
    "company-location": "location",
    "job-salary": "salary_raw",
    "tag-list": "tags",
}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}


class CardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = {name: [] for name in FIELD_CLASSES.values()}
        self.stack = []
        self.href = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        active = self.stack[-1][1] if self.stack else None
        for class_name in classes:
            if class_name in FIELD_CLASSES:
                active = FIELD_CLASSES[class_name]
        if tag == "a" and "job-name" in classes:
            self.href = attrs.get("href", "")
        if tag not in VOID_TAGS:
            self.stack.append((tag, active))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        active = self.stack[-1][1] if self.stack else None
        if active:
            self.parts[active].append(data)

    def fields(self):
        fields = {name: " ".join("".join(parts).split())
                  for name, parts in self.parts.items() if name != "tags"}
        fields["tags"] = [part.strip() for part in self.parts["tags"] if part.strip()]
        fields["education"] = next((tag for tag in fields["tags"] if any(
            word in tag for word in ("本科", "硕士", "博士", "大专", "高中", "中专", "学历")
        )), None)
        fields["experience"] = next((tag for tag in fields["tags"] if any(
            word in tag for word in ("年", "经验", "应届", "在校")
        )), None)
        return fields


def needs_ocr(text):
    return not text.strip() or any(unicodedata.category(char) == "Co" for char in text)


def main():
    html_path = OUTPUT / "raw-first-card.html"
    image_path = OUTPUT / "raw-salary.png"
    parser = CardParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    job = parser.fields()
    if not job["title"] or not parser.href:
        raise ValueError("卡片中缺少岗位标题或链接，请检查保存的 HTML。")

    raw_salary = job["salary_raw"]
    if needs_ocr(raw_salary):
        engine = RapidOCR(params={"Global.log_level": "warning"})
        result = engine(str(image_path), use_det=False, use_cls=False, use_rec=True)
        if not result.txts or not any(text.strip() for text in result.txts):
            raise ValueError("薪资需要 OCR，但识别结果为空。")
        job["salary"] = " ".join(result.txts)
        job["salary_source"] = "OCR"
        job["salary_ocr_scores"] = [float(score) for score in result.scores]
    else:
        job["salary"] = raw_salary
        job["salary_source"] = "DOM"

    job.update(normalize_salary(job["salary"], raw_salary))

    job["url"] = urljoin("https://www.zhipin.com", parser.href)
    job["id"] = PurePosixPath(urlsplit(job["url"]).path).stem
    job["source_html"] = html_path.name
    if job["salary_source"] == "OCR":
        job["source_salary_image"] = image_path.name
    job.pop("salary_raw", None)
    path = OUTPUT / "first-real-job.json"
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    print("岗位：", job["title"])
    print("薪资：", job["salary"], "读取方式：", job["salary_source"])
    print("真实岗位记录已保存：", path)


if __name__ == "__main__":
    main()
