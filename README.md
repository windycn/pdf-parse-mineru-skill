# PDF-parse

PDF-parse 是一个面向 Codex 的 MinerU 精准解析 skill，默认使用 MinerU VLM 高精度模式解析 PDF、Office 文档和图片。它支持中文引导、自动并发批处理、超过 200 页/200MB 的 PDF 自动拆分、多 MinerU API token 轮换，以及解析结果合并。

## 功能

- 默认 `model_version: "vlm"`，适合复杂版式、表格、公式、图片和扫描件。
- 本地文件使用 MinerU 精准 API 批量上传解析，URL 文件使用 MinerU URL 批量解析。
- PDF 超过 200 页或 200MB 时自动拆分，解析后合并 Markdown、JSON、HTML 和 debug PDF。
- 多账号调度：优先使用每个账号每天前 1000 页高优先级额度，全部用完后进入慢速并发模式。
- 配置多个 token 时提醒填写 `api_expires_at`，避免 API 到期后排查困难。
- 日志和报告中会 mask token。

## 快速开始

生成配置模板：

```bash
python scripts/mineru_parse.py --print-config-template > mineru_accounts.yaml
```

编辑 `mineru_accounts.yaml`，填入 token 和到期时间：

```yaml
mineru_accounts:
  - name: account-a
    token: "PASTE_TOKEN_A"
    api_expires_at: "2026-12-31"
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

先 dry-run：

```bash
python scripts/mineru_parse.py paper.pdf docs/*.pdf \
  --accounts mineru_accounts.yaml \
  --out-dir mineru-output \
  --dry-run
```

正式解析：

```bash
python scripts/mineru_parse.py paper.pdf docs/*.pdf \
  --accounts mineru_accounts.yaml \
  --out-dir mineru-output
```

## 依赖

```bash
python3 -m pip install pypdf PyYAML
```

## 输出

合并结果默认位于：

- `mineru-output/merged/full.md`
- `mineru-output/merged/content_list.json`
- `mineru-output/merged/model.json`
- `mineru-output/merged/middle.json`
- `mineru-output/merged/index.html`
- `mineru-output/merged/manifest.md`

## 注意

文档内容会发送到 MinerU API 服务端解析。处理敏感文件前，请确认已经允许使用 MinerU 云端解析。
