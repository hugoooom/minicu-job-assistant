# 自动采集岗位与职位描述

在 minicu 目录运行：

```powershell
.\.venv\Scripts\python.exe collect_jobs.py --keyword Python --city 广州 --limit 3
```

脚本自动启动或复用本地 9333 端口的专用浏览器，创建独立任务标签页，打开搜索结果，读取当前列表中的岗位，必要时对薪资截图 OCR，再逐个打开详情页、校验岗位 URL 中的 ID、提取职位描述。任务标签页结束后自动关闭，专用浏览器保留。

首次登录或登录过期时可能需要在专用浏览器中手动登录；正常状态不需要选择标签页或逐步回车。加 `--no-prompt` 可在列表无法加载时直接退出，适合检查自动流程。

参数：
- `--keyword`：搜索关键词，默认 Python。
- `--city`：广州或深圳，默认广州。
- `--limit`：本轮最多处理 1–10 个岗位，默认 3。

当前只处理首次加载的搜索结果，尚未实现翻页、持续滚动或自动投递。

结果按运行时间分别放在 `output/runs/时间/jobs.json`，同目录保存卡片 HTML、需要 OCR 的薪资截图和职位描述文本。`description_status` 为 `ok` 才表示本次成功取得描述；`error`、`blocked`、`pending` 均不代表成功。整体 `status` 可能是 `complete`、`partial` 或 `stopped`。

职位描述通过 CDP 的 DOMSnapshot 读取渲染文本，过滤隐藏节点及其后代，避免把页面 HTML 中的隐藏干扰字混入正文。脚本全程不启用 Runtime，也不执行页面 JavaScript。

实现阅读顺序：`main` → `read_list` → `read_description` → `rendered_text`。实际行号以当前文件为准。

本版本已于 2026-10-31 在当前机器实测：广州、Python、连续 3 个岗位，均取得职位描述。这个结果只覆盖当时页面和登录状态，选择器或页面机制变化时仍需重新验证。
