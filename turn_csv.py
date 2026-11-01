import json
from pathlib import Path
import pandas as pd
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output",default="out.csv")
args = parser.parse_args()
path = Path(args.input)

def read_jobs(path):
    with open(path, 'r', encoding="utf-8") as f:
        data=json.load(f)
        return data['jobs']
jobs = read_jobs(path)

df = pd.DataFrame(jobs)

columns = [
    "title",
    "company",
    "location",
    "salary",
    "description",
    "description_status",
]
df = df[columns]
df.to_csv(args.output, index=False, encoding="utf-8-sig")
