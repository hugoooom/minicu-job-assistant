import argparse
import json
from pathlib import Path
import pandas as pd


COLUMNS = [
    "title",
    "company",
    "location",
    "salary",
    "description",
    "description_status",
]


def read_jobs(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)["jobs"]


def export_csv(input_path, output_path=None):
    path = Path(input_path)
    output = Path(output_path) if output_path is not None else path.with_suffix(".csv")
    if output.resolve() == path.resolve():
        raise ValueError("CSV 输出路径不能与输入 JSON 相同。")
    jobs = read_jobs(path)
    # 空批次保留表头；失败岗位缺失的字段导出为空，不丢弃记录。
    df = pd.DataFrame(jobs).reindex(columns=COLUMNS)
    df.to_csv(output, index=False, encoding="utf-8-sig")
    return output


def main():
    parser = argparse.ArgumentParser(description="将岗位 JSON 导出为 CSV 表格。")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", help="默认与输入 JSON 同目录、同名，扩展名为 .csv")
    args = parser.parse_args()
    print("表格已保存：", export_csv(args.input, args.output))


if __name__ == "__main__":
    main()
