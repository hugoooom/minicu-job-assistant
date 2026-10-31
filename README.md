# MiniCU Job Assistant

使用原生 Chrome DevTools Protocol（CDP）读取招聘页面，结合 DOM 和局部 OCR 提取薪资，自动打开岗位详情并保存职位描述。

当前实现面向 Windows + Microsoft Edge，适合学习浏览器自动化、数据提取和后续求职筛选。

## 功能

- 独立启动或复用专用 Edge，使用单独的浏览器资料目录。
- 按关键词和城市自动搜索，无需手动选择标签页。
- 读取首次加载的岗位列表，按岗位 ID 去重。
- 薪资为空或含 Unicode 私用区字符时，对局部截图进行单行 OCR。
- 自动进入详情，核对 URL 中的岗位 ID，读取职位描述。
- 按渲染状态过滤隐藏文本，避免 HTML 中的隐藏干扰字混入正文。
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
| `--limit` | 首次加载列表中最多处理 1–10 个岗位，默认 3 |
| `--no-prompt` | 列表无法加载时直接退出，不等待人工操作 |

```powershell
.\.venv\Scripts\python.exe collect_jobs.py --keyword "AI 实习" --city 深圳 --limit 5
```

结果保存在 `output/runs/运行时间/jobs.json`，同目录包含卡片 HTML、需要 OCR 的薪资截图及职位描述文本。

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

## 代码阅读顺序

| 文件 | 用途 |
|---|---|
| `collect_jobs.py` | 自动采集、薪资 OCR、职位描述提取 |
| `extract_first_job.py` | 卡片字段解析及 OCR 回退判断 |
| `inspect_cdp.py` | 独立 Edge 启动和端口检查；另含 Playwright 对照实验 |
| `inspect_raw_cdp.py` | 原生 CDP 客户端及诊断实验 |
| `AUTO_COLLECT.md` | 使用说明 |
| `tests/test_core.py` | 核心逻辑的离线测试 |

自动采集路径不使用 Playwright 连接浏览器。依赖中保留 Playwright，是因为启动帮助函数目前与早期对照实验共用模块。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

离线测试使用虚构样例，不访问招聘平台。本版本于 2026-10-31 在开发机器完成连续 3 个岗位的实际采集与职位描述读取；其他环境、页面版本和登录状态仍需验证。

## 当前边界

- 只处理首次加载的列表，尚未实现翻页、持续滚动和自动投递。
- OCR 结果需要核对，置信分数不等于准确率保证。
- 职位描述至少 40 字符才接受，可能跳过真实但很短的描述；该阈值不保证内容完整。
- 只读取当前账户正常可见的内容，遇到登录墙或明确访问限制会停止。
- 页面变化时可能需要调整选择器，使用前需核对目标网站访问规则。
- `output/` 包含登录状态与采集结果，仅保存在本地，已由 Git 忽略规则排除。

## 参考

- [boss-zhipin-scraper](https://github.com/eatmoreduck/boss-zhipin-scraper)：独立浏览器、CDP 连接与岗位字段组织的参考。
- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/)：DOM、DOMSnapshot、Page 等接口。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：薪资文字识别。

本项目目前采用 DOM + OCR 路径，未实现参考项目的网络响应采集，也未实现完整 GKD 选择器或 LLM Agent。
