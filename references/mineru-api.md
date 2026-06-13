# MinerU 精准解析 API 摘要

来源：`https://mineru.net/apiManage/docs` 和 `https://opendatalab.github.io/MinerU/reference/output_files/`。实现前如发现接口异常，先重新核对官方文档。

## 模式和限制

- 精准解析 API 需要 `Authorization: Bearer <token>`。
- 模型版本支持 `pipeline`、`vlm`、`MinerU-HTML`；本 skill 默认 `vlm`。
- 官方 ecosystem skill 还提供 `mineru-open-api flash-extract` 作为免 token 快速模式；它适合 10MB/20 页以内的简单 Markdown 转换。本 skill 聚焦 token 精准解析和批量自动化。
- 单文件限制：不超过 200MB，不超过 200 页。
- 本地批量上传一次最多申请 50 个文件上传链接。
- URL 批量提交一次最多 50 个文件。
- 每个账号每天前 1000 页为最高优先级；超过后优先级降低。官方文档未提供额度查询接口。
- 输出是 zip 包，默认包含 Markdown 和 JSON，可通过 `extra_formats` 请求 `docx`、`html`、`latex`。
- 文档会上传或交由 MinerU API 读取并在服务端解析；处理敏感材料前确认用户授权。
- VLM 更适合复杂版式高精度解析；pipeline 在官方 ecosystem 说明中被描述为更偏稳定、低幻觉风险。

## 支持格式与语言

精准解析支持 PDF、图片（png/jpg/jpeg/jp2/webp/gif/bmp）、Doc、Docx、Ppt、PPTx、Xls、Xlsx；HTML 需要使用 `MinerU-HTML` 模型。

常用 `language`：

- `ch`：中英文，默认值。
- `ch_server`：中英文、繁体、日文，适合繁体或手写体场景。
- `en`：英文。
- `japan`：日文为主。
- `korean`：韩文。
- `chinese_cht`：繁体中文为主。
- `latin`、`arabic`、`cyrillic`、`east_slavic`、`devanagari`：对应语系包。

## 本地文件批量上传

申请上传 URL：

```http
POST https://mineru.net/api/v4/file-urls/batch
Authorization: Bearer <token>
Content-Type: application/json
```

请求主体：

```json
{
  "files": [
    {"name": "demo.pdf", "data_id": "demo"}
  ],
  "model_version": "vlm",
  "language": "ch",
  "enable_table": true,
  "enable_formula": true,
  "extra_formats": ["docx", "html", "latex"]
}
```

响应 `data.batch_id` 和 `data.file_urls`。随后对每个 `file_url` 执行 `PUT` 上传原文件内容；上传时不要设置 `Content-Type`。上传完成后不需要再次提交任务，MinerU 会自动检测并提交解析。

## URL 批量提交

```http
POST https://mineru.net/api/v4/extract/task/batch
Authorization: Bearer <token>
Content-Type: application/json
```

请求主体：

```json
{
  "files": [
    {"url": "https://cdn-mineru.openxlab.org.cn/demo/example.pdf", "data_id": "demo"}
  ],
  "model_version": "vlm"
}
```

可用参数包括 `language`、`enable_table`、`enable_formula`、`extra_formats`、`no_cache`、`cache_tolerance`；每个 file 可设置 `is_ocr`、`data_id`、`page_ranges`。

## 批量结果轮询

```http
GET https://mineru.net/api/v4/extract-results/batch/{batch_id}
Authorization: Bearer <token>
```

返回 `data.extract_result` 列表。常见状态：

- `waiting-file`：等待文件上传后排队提交。
- `pending`：排队中。
- `running`：解析中，可读 `extract_progress.extracted_pages` 和 `total_pages`。
- `converting`：格式转换中。
- `done`：完成，读取 `full_zip_url`。
- `failed`：失败，读取 `err_msg`。

## 输出文件

zip 中通常包括：

- `full.md`：Markdown 解析结果。
- `*_model.json`：模型推理结果。VLM 后端为“页列表 -> 块列表”的二级数组。
- `*_middle.json`：中间处理结果，常含 `pdf_info`。
- `*_content_list.json` 和可能存在的 `*_content_list_v2.json`：按阅读顺序组织的内容列表。
- `*_layout.pdf`：版面可视化 debug PDF。
- HTML 文件解析时 `full.md` 为 Markdown，`main.html` 为正文 HTML。

VLM 输出与 pipeline 输出不完全兼容。合并 JSON 时应按分片顺序累加页码，并调整所有 `page_idx`。

## 常见错误

- `A0202`：Token 错误，检查 Bearer 前缀或换 token。
- `A0211`：Token 过期，换 token。
- `-60002`：文件格式检测失败，确认扩展名和实际格式。
- `-60005`：文件超过 200MB。
- `-60006`：文件超过 200 页，需要拆分。
- `-60008`：URL 读取超时，优先下载到本地上传。
- `-60009`：任务提交队列已满，稍后重试。
- `-60018`：每日解析任务数量达上限。
