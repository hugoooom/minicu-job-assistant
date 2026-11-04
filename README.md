# MiniCU Job Assistant

使用原生 Chrome DevTools Protocol（CDP）读取招聘页面，结合 DOM 和局部 OCR 提取薪资，自动打开岗位详情并保存职位描述。

当前实现面向 Windows + Microsoft Edge，适合学习浏览器自动化、数据提取和后续求职筛选。

当前为早期测试版，支持广州、深圳搜索，需要在本机专用 Edge 中手动登录。建议首次采集 3 条，核对 JSON 和 CSV 后再增加数量。实测范围与已知问题见 [TESTING.md](TESTING.md)。

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
git clone https://github.com/hugoooom/minicu-job-assistant.git
cd minicu-job-assistant
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "import onnxruntime; print(onnxruntime.__version__)"
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 3
```

自动采集入口使用系统 Edge，不需要额外下载 Playwright Chromium。首次 OCR 运行可能下载模型。

首次登录或登录过期时，需要在专用浏览器中手动完成登录。正常登录状态下，搜索、读取列表、打开详情和保存结果均自动完成。

页面切换时账号标志可能晚于岗位内容加载。列表、正文及历史薪资补采会等待账号标志就绪；等待超时记录为该页面读取失败，不把“暂未检测到”直接解释成登录失效，也不读取未确认登录的预览内容。明确出现登录提示或安全验证时仍暂停处理。

### Windows 依赖排查

`ImportError: onnxruntime is not installed.` 表示运行脚本的 Python 无法加载 OCR 推理依赖。只安装 `rapidocr` 不足以准备本项目环境；完整依赖已列在 `requirements.txt`。请始终用同一个 Python 安装和运行，推荐使用上面的 `.venv` 命令。下载 ZIP 的用户请在解压后包含 `requirements.txt` 的项目目录打开 PowerShell。

若仍报错，先直接导入查看底层异常：

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); import onnxruntime; print(onnxruntime.__version__)"
.\.venv\Scripts\python.exe -m pip check
```

如果提示缺少模块，用该环境重新执行 `-m pip install -r requirements.txt`；若提示 `DLL load failed`，按 [ONNX Runtime 官方安装说明](https://onnxruntime.ai/docs/install/) 检查 Windows Visual C++ 运行库及 Python 架构。建议使用 64 位 Python 3.12 或 3.13。

程序会在打开任务浏览器前检查 `onnxruntime` 的实际导入与可用推理提供程序。程序正常结束、出错停止或按 Ctrl+C 后，默认保留任务标签页，方便检查最后停留的位置和网站提示。只有显式指定 `--close-tab` 时才关闭自己创建的任务标签页；这与程序运行中网页跳转到 `about:blank` 是两种现象。保留页面不等于自动断点续采，下次运行目前仍会创建新的任务标签页。

| 参数 | 说明 |
|---|---|
| `--keyword` | 搜索关键词，默认 Python |
| `--city` | 广州或深圳，默认广州 |
| `--limit` | 本轮新增目标数，默认 200，必须为正整数；默认完成当前区后再判断，实际条数可超过目标；历史补采不占新增名额 |
| `--hard-limit` | 严格达到条数就停止，适合少量验证；默认不启用。`--citywide` 始终严格按条数限制 |
| `--empty-rounds` | 连续多少轮未看到不同岗位时结束该区，默认 3；新看到的历史岗位也算列表在前进，避免误跳过后面新岗位 |
| `--restart-districts` | 忽略续采位置，从指定范围的第一个区重新开始；保留历史去重 |
| `--db` | 自动导入的 SQLite 文件，默认 `output/jobs.db` |
| `--skip-repair` | 本轮跳过数据库中缺失薪资/正文的 URL 补采；默认先修复再采新岗位 |
| `--repair-limit` | 每轮历史补采最多处理的不同岗位网址数，默认 30；普通补采和地址补采共用此预算，剩余留待后续运行 |
| `--update-locations` | 只处理所选城市的历史工作地址补采，不采新增列表；已有缺失薪资/正文也会顺便修复 |
| `--district` | 只采指定区，可传多个，如 `--district 荔湾区 天河区`；默认按所选城市的各区依次采集 |
| `--citywide` | 使用原来的全市单列表；与 `--district` 互斥 |
| `--max-scrolls` | 每区滚动安全上限；默认取 `60` 和 `12 + 向上取整(历史岗位数 / 15)` 中较大值；全市模式沿用后者。整区模式触顶则停止本轮并保留当前区位置，不将该区视为已完成 |
| `--close-tab` | 结束后关闭程序创建的任务标签页；默认保留 |
| `--no-prompt` | 未确认登录时直接退出，不采集游客数据 |
| `--include-seen` | 允许再次采集历史岗位，用于复测；本轮内仍按名称与公司去重 |

```powershell
.\.venv\Scripts\python.exe collect_jobs.py --keyword "AI 实习" --city 深圳 --limit 5
```

例如以 20 条为目标（仍先完成当前区；严格只取 20 条时加 `--hard-limit`）：

```powershell
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 20
```

结果保存在 `output/runs/运行时间/jobs.json`，采集结束后自动调用 `turn_csv.py` 的导出函数，在同目录生成同名 `jobs.csv`。同目录包含卡片 HTML、需要 OCR 的薪资截图及职位描述文本。

### 按区采集（默认）

默认通过 `multiBusinessDistrict` 依次搜索广州 11 个区或深圳当前配置的 9 个区。
每个区使用独立搜索列表，同一个账号、任务标签页串行处理。默认以 200 条为目标，
即使中途达到目标，也等当前区连续多轮不再出现不同岗位后，处理完详情并入库，再停止。
目标未达到时继续下一区；整区模式遇滚动安全上限则停在当前区，不把截断列表当作已完成。
同城历史和本轮其他区已经保存的岗位都会跳过。
程序核对网址参数、页面城市和整区选中标签；范围未确认时记录该区错误并尝试下一区，
出现登录或访问验证阻断则停止整轮。区域配置见 `search_regions.py`，网站区码或界面变化可能需要更新。

```powershell
# 默认目标 200 条，完成当前区后再停；不足则继续下一区
python collect_jobs.py
# 只采两个区，目标 500 条，至少处理完当前区
python collect_jobs.py --city 广州 --district 荔湾区 天河区 --limit 500
# 严格只采 3 条做验证
python collect_jobs.py --district 荔湾区 --limit 3 --hard-limit
# 深圳分区采集
python collect_jobs.py --city 深圳 --limit 100
```

分区进度保存在 `output/district-progress.json`，按城市、关键词和分区范围分别记录。
下次先复查上次停留的区；旧岗位全部重复时仍继续翻页，直到列表连续多轮没有不同岗位，
再切下一区。剩余区扫完后会轮回前面的区，每轮最多尝试配置范围中的每个区一次。
`--restart-districts` 可以从头开始，但不会取消历史去重。进度文件不保存网站分页游标，
重启后仍需要重新加载当前区的旧列表。只保证处理完本轮可见列表，不保证网站提供了全区所有岗位。

JSON 的 `district_runs` 保存已尝试各区的数量、实际滚动次数、停止原因及错误，
`districts` 保存计划范围，`search_mode` 标识分区/全市模式。
每个岗位的 `search_district`、`search_district_code` 表示首次采到它的搜索区，
不替代 `location` 实际地址；同名同公司的跨区岗位仍按既有规则合并。
`list_scroll_count` 为整轮累计滚动次数，`list_scroll_limit` 为每区上限。
这些分区元信息保存在 JSON；现有 CSV 和 SQLite 的字段结构保持原样。
分区能扩大覆盖范围，但不保证网站返回所有岗位或一定凑够请求条数。

### 分批采集

默认读取本项目 `output/runs/*/jobs.json` 中与本次 `--city` 相同的历史结果，在该城市内按“岗位名称 + 公司名称”组合，跨关键词跳过已采集岗位。历史岗位不占本轮新增目标；默认先完成当前区，所以 `--limit 25` 可能新增超过 25 条。需要精确小批次时使用 `--hard-limit`。程序恢复分区位置后，仍从该区列表开头加载，不依赖可能变化的搜索排序。

名称先统一全角半角、连续空白和英文字母大小写，再精确比较，不做模糊匹配。同一搜索城市内，同名同公司即使链接或 ID 改变也会跳过；同名不同公司、同公司不同岗位分别保留。不同搜索城市的历史互不影响：广州采过的岗位不阻止深圳采集。这里以运行参数 `--city` 为范围，不猜测卡片地点。名称或公司缺失时才退回岗位 ID，避免误合并缺失字段的记录。历史结果缺少城市时，不自动归入某个城市。

列表阶段仍跳过历史标识；但每轮开始会先读取当前 SQLite 中所选城市所有关键词组的岗位，薪资或正文为 NULL/空文本时，使用已存详情 URL 安排补采。只读取缺失字段，保留原有有效字段、原关键词和数据库内部 ID；补采不计入新增目标。薪资需要时使用局部 OCR，正文沿用 DOM/OCR 回退。确认停止招聘、职位关闭/不存在等明确状态时删除并继续下一条；普通超时、无依据跳转或读取失败保留缺失状态，重试间隔到期后再试。登录或访问验证阻断时停止整轮。无有效 BOSS URL 的记录会报告跳过。

为减少重复访问，历史补采采用以下规则（新增岗位目标仍默认为 200）：

- 默认每轮最多安排 30 个不同岗位网址，可用 `--repair-limit 10` 调小。跨关键词的同网址只处理一次，结果分别写回原关键词；已保存的互补字段优先复用。
- 同一页面里的薪资、正文和地址依次读取；一个字段失败也不会为下一个字段重新打开该页面。完成登录后直接继续读取已打开的目标页。
- 补采按“城市 + 岗位 ID + 字段”记录尝试时间。失败或处理中断的字段至少隔 24 小时再试；确认未公开工作地址的页面，其地址字段至少隔 7 天再试。其他到期字段仍可处理，不永久放弃缺失数据。
- 尝试记录存入同一个本地 SQLite 的 `repair_attempts` 表，重启程序继续生效。根 JSON 的 `repair_schedule` 记录冷却和预算暂缓条数，`history_repair.reused_urls` 记录本轮复用次数。
- `--update-locations` 没有到期任务时直接结束，不创建浏览器任务页。此时不会生成新的采集批次。

这些规则减少无效请求，不保证不会触发网站风控。出现安全验证、环境异常或访问频繁提示时仍停止本轮，不自动刷新重试。补采限制不等同于整轮网页访问总上限，新增采集及人工登录可能另外产生访问。

失效识别优先读取目标岗位的状态标签或错误区域，再隔一小段时间重新核对。不会只因 HTTP/页面加载超时、推荐区的关闭岗位或 JD 中提到“停止招聘”等词就删除。删除按所选城市和详情 URL 对应的平台岗位 ID 匹配，清理同 URL 的关键词副本，保留其他城市或不同 URL 的记录。

本地 `deleted_jobs` 表保留删除原因、页面证据和原记录快照；确认失效的条目从 `jobs` 表移除，不再进入补采队列。该事务失败时保留原岗位并记录错误。之后重新导入旧批次，也会按删除记录跳过相同平台岗位，避免复活。修复明细中的 `repair_status=deleted`、`unavailable_evidence` 和根 JSON 的 `history_repair.deleted` 可用于核对；数据库和这些证据都留在被忽略的 `output/` 中。

补采明细保存在本轮目录的 `repairs/关键词摘要/jobs.json`，根 JSON 的 `history_repair` 记录成功、失败、跳过及入库错误；旧批次 JSON 不改写，数据库为最新合并结果。修复成功且两字段均有效的记录不再进入下一轮修复队列。数据库不存在时没有待补采队列；旧 JSON 尚未入库时可先执行 `python job_db.py --input output/runs`。`--skip-repair` 可临时只采新岗位。

如需允许原本完整的历史岗位也再次进入列表采集，可用 `--include-seen`；它允许重复采集，不等同于只补齐空字段。本轮已经安排补采的对象仍不重复计入新增。

```powershell
# 第一批；再次执行相同命令从上次停留的区继续
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 200
# 允许历史岗位再次出现，用于回归测试
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 5 --include-seen
```

`historical_job_count` 为当前城市的历史唯一岗位标识总数，`historical_skipped_count` 为本轮实际遇到并跳过的历史唯一岗位数；`deduplication_fields` 记录名称与公司字段，`deduplication_city` 记录去重的搜索城市范围。默认按历史量估算滚动预算，分区模式至少 60 次；可显式增加 `--max-scrolls`。`list_scroll_limit`、`list_scroll_count` 和 `list_scroll_budget_mode` 分别记录每区上限、整轮累计次数和预算模式。`requested_count` 是新增目标，不是网站岗位总数；全部区正常处理完且所有已采岗位提取成功时，即使不足目标也可以完成。某区加载失败、滚动触顶或提取失败会记录为部分完成。

去重依赖本地历史结果：删除、移动历史 `jobs.json` 会失去对应记录，旧项目目录的结果不会自动导入。岗位名称或公司名称变化时可能视为新岗位。请依次运行批次；并行运行的批次不保证互相排除尚未保存的岗位。

## 工作地址与可视化字段

新采岗位在读取详情时，会尝试读取“工作地址”区域，使用完整地址更新 `location`，
不从公司介绍猜地址。保留 `card_location` 原列表地址，并记录 `location_source`（detail_dom/detail_ocr/card）。
详情地址优先于列表地址：导入旧批次不会把已确认的完整地址覆盖成“广州”这样的简略值。
读取失败时保留原地址。地址 OCR 的复核标记在 JSON 中记录，不能解释成已人工确认。

数据库按需新增以下字段，保留已有岗位及内部 ID：

| 字段 | 含义 |
|---|---|
| `location` | 页面工作地址；尚未补采时可能仍为列表地址 |
| `card_location` | 原始列表地址 |
| `location_city`、`location_district` | 从地址明确解析的实际城市/区；无法识别时为 NULL，不使用搜索城市/区代填 |
| `location_source` | 地址来源，详情 DOM/OCR 优先 |
| `longitude`、`latitude` | 网站地图元素给出的原始经纬度；缺失/非法值保留 NULL |
| `coordinate_system` | 目前为 unknown，网站坐标系未核实，未当成 WGS84 或自动转换 |

批量补采已有岗位（通过原详情 URL，不采新列表、不改变分区续采位置）：

```powershell
python collect_jobs.py --city 广州 --update-locations
```

该模式处理原地址尚不是有效详情来源的记录，并沿用失效岗位删除和登录检查。
猎头/代招页面若正文已稳定、仍没有工作地址，确认短暂稳定后会快速跳过，
记录 `location_status=not_provided` 和 `history_repair.address_not_provided`，保留原地址及岗位。
有工作地址的猎头页面照常读取；普通加载中或尚未确认登录的页面不会直接按“未提供”跳过。未公开地址的结果会使地址补采延后 7 天，避免每次启动都访问同一页面。
更新结果写回 SQLite，明细位于本轮 `repairs/`；根 CSV 只含新增岗位，因此该模式的根 CSV 为空表。
尚未入库的旧 JSON 可先导入再补采。单个岗位多个地址时，当前只保存工作地址区的第一个地址。

例如统计已读取详情地址的岗位分布：

```sql
SELECT location_city, location_district, COUNT(*) AS job_count
FROM jobs
WHERE location_source IN ('detail_dom', 'detail_ocr')
GROUP BY location_city, location_district;
```

地图可视化前需先核实坐标系；现阶段可以直接做已采样本的城市/区分布统计，不能宣称全市场覆盖。

## 导出 CSV

采集结束后自动导出同目录、同名的 CSV；部分完成或停止的批次也导出已保存的岗位，空批次生成只有表头的 CSV。也可单独使用 `turn_csv.py` 导出历史 JSON：

```powershell
.\.venv\Scripts\python.exe turn_csv.py --input "output/runs/运行时间/jobs.json"
.\.venv\Scripts\python.exe turn_csv.py --input "output/runs/运行时间/jobs.json" --output "my_jobs.csv"
```

`--input` 必填；`--output` 可省略，默认与 JSON 同目录、同名，例如 `jobs.json` 对应 `jobs.csv`。导出包含岗位、公司、地点、薪资、职位描述及描述状态，使用 UTF-8 BOM 编码，方便 Excel 识别中文。

缺少字段导出为空，空岗位列表保留表头。CSV 导出失败会明确提示，已保存的 JSON 保留，可关闭占用文件的 Excel 后单独重新导出。导出的 CSV 由 Git 忽略规则排除。

## 本地 SQLite 数据库

`job_db.py` 使用 Python 自带的 SQLite，将多个批次合并到一个数据库文件，默认位置为项目的 `output/jobs.db`。无需 MySQL 服务、账号或密码，也无需安装 PyMySQL。

```powershell
# 只创建数据库与表（导入时也会自动创建）
python job_db.py --init
# 导入一份批次
python job_db.py --input "output/runs/运行时间/jobs.json"
# 一次导入本项目所有历史批次
python job_db.py --input output/runs
# 指定另一个数据库文件
python job_db.py --input output/runs --db "output/test-jobs.db"
```

统一使用一张 `jobs` 表，按“搜索城市 + 搜索关键词 + 岗位标识”合并。岗位标识沿用现有的名称与公司组合，缺少名称/公司时退回平台 ID；内部比较统一全角半角、空白和英文大小写，展示字段保留原文。同组重复导入不会增加行，不同城市或关键词独立保留。

有效的薪资及成功描述按导入顺序更新；失败、空值或尚未处理的结果不覆盖已有有效字段。OCR 描述的待核对标记同时保留。`source_file` 记录最近导入来源，`first_imported_at`、`last_imported_at` 是入库时间，不是岗位发布时间。若需要只保留最新采集版本，请按批次时间顺序导入；目录导入按路径排序。

命令会显示各城市与关键词的数量。可以使用 Navicat 的 SQLite 连接打开 `output/jobs.db`，查询：

```sql
SELECT city, keyword, COUNT(*) AS job_count
FROM jobs
GROUP BY city_key, keyword_key;
```

采集程序现在默认在每区的详情处理完成后自动调用入库工具，并在正常结束、报错停止或 Ctrl+C 保存后再次尝试入库，默认写入 `output/jobs.db`，也可用 `--db` 指定 SQLite 文件。重复导入不会增加重复行，失败/空字段不覆盖已有有效字段。JSON 的 `database.status` 记录入库结果；数据库被占用等错误会明确提示、保留 JSON，退出码为非零，可使用上面的命令重试。旧历史批次若未入库，仍可手动用 `--input output/runs` 一次导入；不会自动迁移 MySQL 数据。历史 JSON 中没有的岗位无法凭空补入某个关键词组；采集端目前仍按城市跨关键词去重，数据库分组不会改变该采集规则。`output/` 已被 Git 忽略，数据库和登录状态都不上传到 GitHub。

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
| `job_db.py` | SQLite 初始化、跨批次合并和分组统计 |
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

2026-11-01 针对一条仅 18 字符的真实职位描述验证：确认 URL、登录状态且文字稳定后正常保存，不再因不足 40 字符超时。此验证只覆盖该条短正文样例。

## 反馈问题

请在 [Issues](https://github.com/hugoooom/minicu-job-assistant/issues) 提供 Windows、Edge、Python 版本、运行参数、完整报错，以及预期结果和实际结果。截图和日志请先遮挡个人信息；不要上传专用浏览器目录、Cookie 或登录凭据。

## 当前边界

- 支持有限次数的滚动加载，尚未实现自动投递；加载不到请求数量会报告缺口，不保证遍历网站全部岗位。
- OCR 结果需要核对，置信分数不等于准确率保证。
- 职位描述不足 40 字符时，确认岗位 URL、登录状态并等待文字稳定至少 1 秒后保存；“加载中”等占位文字不当作正文。文字稳定不保证网站已给出完整内容，仍需核对。
- OCR 正文会标记 `description_source: "OCR"`、`description_ocr_review_required: true`，并提供截图文件名及置信分数。OCR 转录不保证无误；一条岗位识别失败记为该条 error，继续处理其他岗位。登录墙和访问限制仍会中止或等待登录。
- 正文 OCR 最多分成 30 段；截图受遮挡、布局变化或无法推进时，不将残缺文本标为成功。
- 只读取当前账户正常可见的内容，遇到登录墙或明确访问限制会停止。
- 页面变化时可能需要调整选择器，使用前需核对目标网站访问规则。
- `output/` 包含登录状态与采集结果，仅保存在本地，已由 Git 忽略规则排除。

## 参考

- [boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper)：独立浏览器、CDP 连接与岗位字段组织的参考。
- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/)：DOM、DOMSnapshot、Page 等接口。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：薪资文字识别。

