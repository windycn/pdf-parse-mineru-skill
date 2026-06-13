---
name: pdf-parse
description: MinerU VLM high-precision PDF and document parsing workflow with Chinese guidance, local/URL batch submission, automatic PDF splitting for MinerU 200-page/200MB limits, multi-token daily priority quota rotation, slow-quota fallback concurrency, and merged Markdown/JSON/HTML/debug-PDF outputs. Use when Codex needs to parse PDFs or supported office/image documents through MinerU, especially large PDFs, batches, tables, formulas, scanned documents, or multiple MinerU API accounts.
---

# PDF-parse

使用本 skill 通过 MinerU 精准解析 API 批量解析 PDF、Office 文档和图片。默认使用 VLM 高精度模式，所有面向用户的引导、进度和错误说明默认使用中文。

## 首次使用提示

首次在一个任务中使用本 skill 时，先用中文告诉用户：

- 默认使用 MinerU 精准解析 API 的 `vlm` 模型，适合复杂版式、表格、公式、图片和扫描件。
- 支持自动并发批处理；PDF 超过 200 页或 200MB 时会自动拆分后分别解析，再合并结果。
- 支持多个 MinerU API token，会优先使用每个账号每天前 1000 页高优先级额度；全部账号高优先级额度用完后，继续使用慢速额度并可多账号并发。
- 请在提供多个 API token 时同时填写每个 API 的到期时间，避免 token 到期后不知道失败原因。
- token 只写在本地 YAML 配置里，日志和报告必须打码显示。

推荐让用户按这个格式保存账号配置：

```yaml
mineru_accounts:
  - name: account-a
    token: "PASTE_TOKEN_A"
    api_expires_at: "2026-12-31"
    daily_priority_pages: 1000
    priority_concurrency: 2
    slow_concurrency: 1
  - name: account-b
    token: "PASTE_TOKEN_B"
    api_expires_at: "2026-10-15"
    daily_priority_pages: 1000
    priority_concurrency: 2
    slow_concurrency: 1
defaults:
  model_version: "vlm"
  language: "ch"
  enable_table: true
  enable_formula: true
  is_ocr: false
  extra_formats: ["docx", "html", "latex"]
```

## 快速工作流

1. 如用户没有配置文件，先运行模板命令并让用户填入 token：

```bash
python pdf-parse/scripts/mineru_parse.py --print-config-template > mineru_accounts.yaml
```

2. 先 dry-run，确认拆分、账号轮换、到期提醒和输出目录：

```bash
python pdf-parse/scripts/mineru_parse.py paper.pdf docs/*.pdf \
  --accounts mineru_accounts.yaml \
  --out-dir mineru-output \
  --dry-run
```

3. 确认无误后正式解析：

```bash
python pdf-parse/scripts/mineru_parse.py paper.pdf docs/*.pdf \
  --accounts mineru_accounts.yaml \
  --out-dir mineru-output
```

4. 完成后优先查看：

- `mineru-output/merged/full.md`
- `mineru-output/merged/content_list.json`
- `mineru-output/merged/model.json`
- `mineru-output/merged/middle.json`
- `mineru-output/merged/index.html`
- `mineru-output/merged/manifest.md`

## 脚本能力

使用 `scripts/mineru_parse.py` 执行确定性解析流程。常用参数：

```bash
--accounts mineru_accounts.yaml
--out-dir mineru-output
--dry-run
--print-config-template
--state-file mineru_state.json
--language ch
--ocr
--no-table
--no-formula
--extra-formats docx,html,latex
--poll-interval 10
--timeout 7200
--timezone Asia/Shanghai
--no-download-url-pdfs
```

默认行为：

- `model_version` 固定默认为 `vlm`，除非 YAML 中显式改为其他 MinerU 支持值。
- PDF 会按 MinerU 精准 API 限制拆分为不超过 200 页且不超过 200MB 的分片。
- 本地文件走 `POST /api/v4/file-urls/batch` 获取上传 URL，再 `PUT` 上传；上传完成后 MinerU 自动提交解析。
- URL 文件走 `POST /api/v4/extract/task/batch`；远程 PDF 默认会先下载到临时目录以便判断是否需要拆分。
- 通过 `GET /api/v4/extract-results/batch/{batch_id}` 轮询批量结果并下载 `full_zip_url`。
- 解析结果会合并 Markdown、VLM JSON、HTML 和 debug PDF；DOCX/LaTeX 保留分片文件并在 manifest 中说明。

## 多账号额度规则

- 本地状态文件记录每天每个账号已使用的页数，默认按 `Asia/Shanghai` 日期重置。
- 调度顺序为：先使用账号 A 的高优先级剩余额度，再切换账号 B，以此类推。
- 如果某个 PDF 分片跨过账号剩余高优先级额度边界，脚本会继续拆小，避免浪费高优先级页数。
- 所有账号高优先级额度都用完后，进入慢速模式，按每个账号的 `slow_concurrency` 继续并发。
- `api_expires_at` 早于今天的账号会被跳过；14 天内到期会中文预警；缺失到期时间会中文提醒补充。

## 注意事项

- 需要安装 `pypdf` 和 `PyYAML`。缺失时按脚本中文错误提示安装。
- MinerU 官方文档没有提供额度查询接口，本 skill 的每日 1000 页高优先级额度通过本地状态估算。
- 文档内容会发送到 MinerU API 服务端解析；处理敏感文件前先确认用户允许使用 MinerU 云端解析。
- URL 指向 GitHub、AWS 等境外资源时可能被 MinerU 服务端读取超时；遇到这类情况，优先下载到本地再上传。
- 如果用户没有 token 且只是小文件快速转 Markdown，可提醒其官方 `mineru-open-api flash-extract` 是免登录快速模式；本 skill 的主流程仍然面向 token 精准解析、多格式和大批量。
- VLM 更适合复杂版式高精度解析；如果用户特别强调“不要幻觉”而不是复杂版面准确率，可提示其考虑 pipeline。
- 如果需要完整接口字段或错误码，读取 `references/mineru-api.md`。
