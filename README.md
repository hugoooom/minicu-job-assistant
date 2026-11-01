# MiniCU Job Assistant

使用原生 Chrome DevTools Protocol（CDP）读取招聘页面，结合 DOM 和局部 OCR 提取薪资，自动打开岗位详情并保存职位描述。

当前实现面向 Windows + Microsoft Edge，适合学习浏览器自动化、数据提取和后续求职筛选。

## 功能

- 独立启动或复用专用 Edge，使用单独的浏览器资料目录。
- 按关键词和城市自动搜索，无需手动选择标签页。
- 在采集前确认登录；游客列表出现也不会继续采集。中途登录过期会提示登录并重试当前岗位。
- 读取列表并按需滚动加载，按“岗位名称 + 公司名称”在本轮及历史批次间去重；连续无新岗位标识或达到滚动上限时报告原因。
- 薪资为空或含 Unicode 私用区字符时，对局部截图进行单行 OCR。
- 截图先等待滚动和布局稳定，将视口坐标换算为文档坐标；同时保存 `.capture.json` 坐标记录，便于排查滚动后的错位。
- 从 DOM 保留明确的 K、元/天、几薪等信息，补回 OCR 漏掉的单位与后缀；单位无法确认时明确标为未知。
- 自动进入详情，核对 URL 中的岗位 ID，读取职位描述。
- 按渲染状态过滤隐藏文本，避免 HTML 中的隐藏干扰字混入正文。
- 职位正文仍含混淆字符时，自动定位正文容器、分段滚动截图并进行多行 OCR；按位置合并重叠区，保留截图供核对。
- 逐条保存结果，记录失败原因，区分成功、部分完成和停止。

## 安装与运行

需要 Python 3.12 或 3.13，以及 Microsoft Edge。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 3
```

自动采集入口使用系统 Edge，不需要额外下载 Playwright Chromium。首次 OCR 运行可能下载模型。

首次登录或登录过期时，需要在专用浏览器中手动完成登录。正常登录状态下，搜索、读取列表、打开详情和保存结果均自动完成。

| 参数 | 说明 |
|---|---|
| `--keyword` | 搜索关键词，默认 Python |
| `--city` | 广州或深圳，默认广州 |
| `--limit` | 本轮最多处理 1–100 个岗位，默认 3 |
| `--max-scrolls` | 最多滚动加载次数，默认 12；连续三轮无新增时提前结束 |
| `--no-prompt` | 未确认登录时直接退出，不采集游客数据 |
| `--include-seen` | 允许再次采集历史岗位，用于复测；本轮内仍按名称与公司去重 |

```powershell
.\.venv\Scripts\python.exe collect_jobs.py --keyword "AI 实习" --city 深圳 --limit 5
```

例如采集 20 条：

```powershell
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 20
```

结果保存在 `output/runs/运行时间/jobs.json`，同目录包含卡片 HTML、需要 OCR 的薪资截图及职位描述文本。

### 分批采集

默认读取本项目 `output/runs/*/jobs.json` 中与本次 `--city` 相同的历史结果，在该城市内按“岗位名称 + 公司名称”组合，跨关键词跳过已采集岗位。每次运行 `--limit 25` 都是最多新增 25 个岗位，历史岗位不占本轮名额；程序从搜索页开头重新浏览，遇到历史标识会跳过并继续滚动，不依赖可能变化的搜索排序。

名称先统一全角半角、连续空白和英文字母大小写，再精确比较，不做模糊匹配。同一搜索城市内，同名同公司即使链接或 ID 改变也会跳过；同名不同公司、同公司不同岗位分别保留。不同搜索城市的历史互不影响：广州采过的岗位不阻止深圳采集。这里以运行参数 `--city` 为范围，不猜测卡片地点。名称或公司缺失时才退回岗位 ID，避免误合并缺失字段的记录。历史结果缺少城市时，不自动归入某个城市。

历史记录中薪资或描述失败、尚未处理的岗位也会跳过。如需重新测试这些岗位的提取效果，使用 `--include-seen`；它允许重新采集所有历史岗位，不是只重试失败项。历史结果仍保留在原运行目录。

```powershell
# 第一批；再次执行相同命令收集下一批新岗位
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 25
# 允许历史岗位再次出现，用于回归测试
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 5 --include-seen
```

`historical_job_count` 为当前城市的历史唯一岗位标识总数，`historical_skipped_count` 为本轮实际遇到并跳过的历史唯一岗位数；`deduplication_fields` 记录名称与公司字段，`deduplication_city` 记录去重的搜索城市范围。列表没有更多新岗位或达到滚动上限时，会报告不足条数，不用重复岗位补齐。后续批次可能需要适当增加 `--max-scrolls`。

去重依赖本地历史结果：删除、移动历史 `jobs.json` 会失去对应记录，旧项目目录的结果不会自动导入。岗位名称或公司名称变化时可能视为新岗位。请依次运行批次；并行运行的批次不保证互相排除尚未保存的岗位。

## 导出 CSV

使用 `turn_csv.py` 将某次采集的 `jobs.json` 导出为表格：

```powershell
.\.venv\Scripts\python.exe turn_csv.py --input "output/runs/运行时间/jobs.json"
.\.venv\Scripts\python.exe turn_csv.py --input "output/runs/运行时间/jobs.json" --output "my_jobs.csv"
```

`--input` 必填；`--output` 可省略，默认在当前工作目录生成 `out.csv`。导出包含岗位、公司、地点、薪资、职位描述及描述状态，使用 UTF-8 BOM 编码，方便 Excel 识别中文。

当前要求输入的岗位列表非空，且每条记录对应的表格中具备上述字段。导出的 CSV 由 Git 忽略规则排除。

## 输出

以下为虚构样例，仅展示字段：

```json
{
  "title": "Python 开发实习生",
  "company": "示例公司",
  "location": "广州",
  "id": "example-job-id",
  "url": "https://www.zhipin.com/job_detail/example-job-id.html",
  "salary": "150–200 元/天",
  "salary_source": "OCR",
  "description": "岗位职责：参与后端开发。任职要求：掌握 Python 与基本 SQL。",
  "description_source": "CDP_DOMSnapshot",
  "description_status": "ok"
}
```

薪资原始混淆字符只参与内部判断，不写入岗位 JSON。
整体 `status` 为 `complete`、`partial` 或 `stopped`。单个岗位的 `description_status` 为 `ok` 才表示本次取得描述。

额外字段包括学历 `education`、经验 `experience`、原始要求标签 `tags`，以及 `salary_unit`、`salary_period`、`salary_pay_months`、`salary_status`。页面未给出或无法确认的内容为 `null`，不推断缺失事实。K 表示金额量级，不据此擅自填入月/年周期。

顶层报告 `requested_count`、`collected_count`、`missing_count`、`salary_success_count`、`description_success_count` 和 `fully_extracted_count`。`complete` 要求达到请求条数，且这些岗位的薪资和描述都成功；单位未知或金额识别失败会导致 `partial`。停止时未处理的描述会明确标记 `not_processed`，不再默默保留 pending。

## 代码阅读顺序

| 文件 | 用途 |
|---|---|
| `collect_jobs.py` | 自动采集、薪资 OCR、职位描述提取 |
| `extract_first_job.py` | 卡片字段解析及 OCR 回退判断 |
| `salary.py` | 薪资格式、单位和发薪后缀整理 |
| `job_history.py` | 岗位名称与公司名称标准化、跨批次历史恢复 |
| `description_ocr.py` | 正文分段截图、多行 OCR 和重叠区合并 |
| `inspect_cdp.py` | 独立 Edge 启动和端口检查；另含 Playwright 对照实验 |
| `inspect_raw_cdp.py` | 原生 CDP 客户端及诊断实验 |
| `AUTO_COLLECT.md` | 使用说明 |
| `tests/` | 核心逻辑、登录门槛、薪资完整性和滚动加载的离线测试 |

自动采集路径不使用 Playwright 连接浏览器。依赖中保留 Playwright，是因为启动帮助函数目前与早期对照实验共用模块。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

离线测试使用虚构样例，不访问招聘平台。本版本于 2026-10-31 在开发机器完成连续 3 个岗位的实际采集与职位描述读取；其他环境、页面版本和登录状态仍需验证。

## 当前边界

- 支持有限次数的滚动加载，尚未实现自动投递；加载不到请求数量会报告缺口，不保证遍历网站全部岗位。
- OCR 结果需要核对，置信分数不等于准确率保证。
- 职位描述至少 40 字符才接受，可能跳过真实但很短的描述；该阈值不保证内容完整。
- OCR 正文会标记 `description_source: "OCR"`、`description_ocr_review_required: true`，并提供截图文件名及置信分数。OCR 转录不保证无误；一条岗位识别失败记为该条 error，继续处理其他岗位。登录墙和访问限制仍会中止或等待登录。
- 正文 OCR 最多分成 30 段；截图受遮挡、布局变化或无法推进时，不将残缺文本标为成功。
- 只读取当前账户正常可见的内容，遇到登录墙或明确访问限制会停止。
- 页面变化时可能需要调整选择器，使用前需核对目标网站访问规则。
- `output/` 包含登录状态与采集结果，仅保存在本地，已由 Git 忽略规则排除。

## 参考

- [boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper)：独立浏览器、CDP 连接与岗位字段组织的参考。
- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/)：DOM、DOMSnapshot、Page 等接口。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：薪资文字识别。

本项目目前采用 DOM + OCR 路径，未实现参考项目的网络响应采集，也未实现完整 GKD 选择器或 LLM Agent。
