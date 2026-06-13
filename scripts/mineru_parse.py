#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - checked at runtime
    yaml = None

try:
    from pypdf import PdfReader, PdfWriter
except Exception:  # pragma: no cover - checked at runtime
    PdfReader = None
    PdfWriter = None


API_BASE = "https://mineru.net"
LOCAL_BATCH_ENDPOINT = f"{API_BASE}/api/v4/file-urls/batch"
URL_BATCH_ENDPOINT = f"{API_BASE}/api/v4/extract/task/batch"
BATCH_RESULT_ENDPOINT = f"{API_BASE}/api/v4/extract-results/batch"
MAX_PAGES = 200
MAX_BYTES = 200 * 1024 * 1024
BATCH_LIMIT = 50
TRANSIENT_HTTP = {408, 409, 425, 429, 500, 502, 503, 504}
RUNNING_STATES = {"waiting-file", "pending", "running", "converting"}
TERMINAL_STATES = {"done", "failed"}


CONFIG_TEMPLATE = """mineru_accounts:
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
"""


FIRST_USE_GUIDE = """PDF-parse 使用提示：
- 默认使用 MinerU 精准解析 API 的 VLM 高精度模式，适合复杂版式、表格、公式、图片和扫描件。
- 支持自动并发批处理；PDF 超过 200 页或 200MB 会自动拆分，解析完成后合并 Markdown、JSON、HTML 和 debug PDF。
- 支持多个 MinerU API token，会优先使用每个账号每天前 1000 页高优先级额度；全部用完后切换慢速额度并继续并发。
- 提供多个 API token 时，请务必同时填写每个 API 的到期时间 api_expires_at，避免 token 到期后不知道失败原因。
- token 只保存在本地 YAML 中，日志和报告会打码显示。
"""


class MineruError(RuntimeError):
    pass


class MineruApiError(MineruError):
    def __init__(self, code: Any, msg: str, trace_id: str | None = None):
        super().__init__(f"MinerU API 错误 code={code}, msg={msg}, trace_id={trace_id or '-'}")
        self.code = code
        self.msg = msg
        self.trace_id = trace_id


@dataclasses.dataclass
class Account:
    name: str
    token: str
    api_expires_at: str | None = None
    daily_priority_pages: int = 1000
    priority_concurrency: int = 2
    slow_concurrency: int = 1


@dataclasses.dataclass
class Defaults:
    model_version: str = "vlm"
    language: str = "ch"
    enable_table: bool = True
    enable_formula: bool = True
    is_ocr: bool = False
    extra_formats: list[str] = dataclasses.field(default_factory=lambda: ["docx", "html", "latex"])
    no_cache: bool = False
    cache_tolerance: int = 900


@dataclasses.dataclass
class Chunk:
    chunk_id: str
    original_label: str
    original_index: int
    source_kind: str  # local or url
    file_name: str
    pages: int
    data_id: str
    is_pdf: bool = False
    local_path: Path | None = None
    upload_path: Path | None = None
    url: str | None = None
    page_start: int | None = None
    page_end: int | None = None


@dataclasses.dataclass
class Assignment:
    chunk: Chunk
    account: Account
    mode: str  # priority or slow
    result_dir: Path | None = None
    zip_path: Path | None = None
    state: str = "planned"
    error: str = ""


@dataclasses.dataclass
class BatchJob:
    account: Account
    mode: str
    source_kind: str
    assignments: list[Assignment]


def log(message: str) -> None:
    print(message, flush=True)


def require_yaml() -> None:
    if yaml is None:
        raise MineruError("缺少 PyYAML：请先运行 `python3 -m pip install PyYAML`。")


def require_pypdf() -> None:
    if PdfReader is None or PdfWriter is None:
        raise MineruError("缺少 pypdf：请先运行 `python3 -m pip install pypdf`。")


def is_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"}


def is_pdf_name(value: str) -> bool:
    path = urllib.parse.urlparse(value).path if is_url(value) else value
    return path.lower().endswith(".pdf")


def safe_stem(name: str, fallback: str = "file") -> str:
    stem = Path(urllib.parse.urlparse(name).path).stem if is_url(name) else Path(name).stem
    stem = stem or fallback
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_")
    return stem[:80] or fallback


def safe_data_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    return value[:128] or "item"


def mask_token(token: str) -> str:
    if len(token) <= 10:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def today_in_timezone(timezone_name: str) -> dt.date:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(timezone_name)).date()
    except Exception:
        return dt.date.today()


def parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def load_config(path: Path) -> tuple[list[Account], Defaults]:
    require_yaml()
    if not path.exists():
        raise MineruError(f"找不到账号配置文件：{path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw_accounts = data.get("mineru_accounts") or []
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise MineruError("YAML 中需要包含非空的 mineru_accounts 列表。")

    accounts: list[Account] = []
    for index, item in enumerate(raw_accounts, start=1):
        if not isinstance(item, dict):
            raise MineruError(f"第 {index} 个账号配置不是对象。")
        token = str(item.get("token") or "").strip()
        if not token:
            raise MineruError(f"第 {index} 个账号缺少 token。")
        accounts.append(
            Account(
                name=str(item.get("name") or f"account-{index}"),
                token=token,
                api_expires_at=str(item.get("api_expires_at")) if item.get("api_expires_at") else None,
                daily_priority_pages=int(item.get("daily_priority_pages", 1000)),
                priority_concurrency=max(1, int(item.get("priority_concurrency", 2))),
                slow_concurrency=max(1, int(item.get("slow_concurrency", 1))),
            )
        )

    raw_defaults = data.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        raw_defaults = {}
    defaults = Defaults(
        model_version=str(raw_defaults.get("model_version", "vlm")),
        language=str(raw_defaults.get("language", "ch")),
        enable_table=bool(raw_defaults.get("enable_table", True)),
        enable_formula=bool(raw_defaults.get("enable_formula", True)),
        is_ocr=bool(raw_defaults.get("is_ocr", False)),
        extra_formats=list(raw_defaults.get("extra_formats", ["docx", "html", "latex"]) or []),
        no_cache=bool(raw_defaults.get("no_cache", False)),
        cache_tolerance=int(raw_defaults.get("cache_tolerance", 900)),
    )
    return accounts, defaults


def apply_arg_overrides(defaults: Defaults, args: argparse.Namespace) -> Defaults:
    result = dataclasses.replace(defaults)
    if args.language:
        result.language = args.language
    if args.ocr:
        result.is_ocr = True
    if args.no_table:
        result.enable_table = False
    if args.no_formula:
        result.enable_formula = False
    if args.extra_formats is not None:
        result.extra_formats = [x.strip() for x in args.extra_formats.split(",") if x.strip()]
    if args.no_cache:
        result.no_cache = True
    if args.cache_tolerance is not None:
        result.cache_tolerance = args.cache_tolerance
    return result


def account_expiry_warnings(accounts: list[Account], today: dt.date) -> tuple[list[Account], list[str]]:
    active: list[Account] = []
    warnings: list[str] = []
    for account in accounts:
        expiry = parse_date(account.api_expires_at)
        if not account.api_expires_at:
            warnings.append(f"账号 {account.name} 未填写 api_expires_at，建议补充 API 到期时间。")
            active.append(account)
            continue
        if expiry is None:
            warnings.append(f"账号 {account.name} 的 api_expires_at 格式无法识别：{account.api_expires_at}，请使用 YYYY-MM-DD。")
            active.append(account)
            continue
        days_left = (expiry - today).days
        if days_left < 0:
            warnings.append(f"账号 {account.name} 的 API 已于 {expiry.isoformat()} 到期，已跳过。")
            continue
        if days_left <= 14:
            warnings.append(f"账号 {account.name} 的 API 将于 {expiry.isoformat()} 到期，还剩 {days_left} 天。")
        active.append(account)
    return active, warnings


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"days": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("days", {})
            return data
    except Exception:
        pass
    return {"days": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def day_bucket(state: dict[str, Any], day: dt.date) -> dict[str, Any]:
    days = state.setdefault("days", {})
    bucket = days.setdefault(day.isoformat(), {"accounts": {}})
    bucket.setdefault("accounts", {})
    return bucket


def priority_used(state: dict[str, Any], day: dt.date, account_name: str) -> int:
    bucket = day_bucket(state, day)
    account = bucket["accounts"].setdefault(account_name, {"priority_pages_used": 0, "slow_pages_used": 0})
    return int(account.get("priority_pages_used", 0))


def add_usage(state: dict[str, Any], day: dt.date, account_name: str, mode: str, pages: int) -> None:
    bucket = day_bucket(state, day)
    account = bucket["accounts"].setdefault(account_name, {"priority_pages_used": 0, "slow_pages_used": 0})
    key = "priority_pages_used" if mode == "priority" else "slow_pages_used"
    account[key] = int(account.get(key, 0)) + int(pages)


def read_pdf_page_count(path: Path) -> int:
    require_pypdf()
    reader = PdfReader(str(path))
    return len(reader.pages)


def write_pdf_slice(source: Path, start_page: int, end_page: int, output: Path) -> Path:
    require_pypdf()
    output.parent.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page_index in range(start_page - 1, end_page):
        writer.add_page(reader.pages[page_index])
    with output.open("wb") as f:
        writer.write(f)
    return output


def make_pdf_chunk(
    source: Path,
    original_label: str,
    original_index: int,
    start_page: int,
    end_page: int,
    upload_path: Path | None,
) -> Chunk:
    stem = safe_stem(original_label, f"input-{original_index}")
    suffix = f"p{start_page:04d}-{end_page:04d}"
    file_name = f"{stem}_{suffix}.pdf"
    chunk_id = safe_data_id(f"{original_index:03d}-{stem}-{suffix}")
    return Chunk(
        chunk_id=chunk_id,
        original_label=original_label,
        original_index=original_index,
        source_kind="local",
        file_name=file_name,
        pages=end_page - start_page + 1,
        data_id=chunk_id,
        is_pdf=True,
        local_path=source,
        upload_path=upload_path,
        page_start=start_page,
        page_end=end_page,
    )


def split_pdf_by_limits(source: Path, original_label: str, original_index: int, split_dir: Path) -> list[Chunk]:
    require_pypdf()
    total_pages = read_pdf_page_count(source)
    chunks: list[Chunk] = []
    stem = safe_stem(original_label, f"input-{original_index}")

    def emit_range(start_page: int, end_page: int) -> None:
        page_count = end_page - start_page + 1
        if page_count > MAX_PAGES:
            cursor = start_page
            while cursor <= end_page:
                sub_end = min(cursor + MAX_PAGES - 1, end_page)
                emit_range(cursor, sub_end)
                cursor = sub_end + 1
            return

        use_original = start_page == 1 and end_page == total_pages and source.stat().st_size <= MAX_BYTES
        candidate = source if use_original else split_dir / f"{stem}_p{start_page:04d}-{end_page:04d}.pdf"
        if not use_original:
            write_pdf_slice(source, start_page, end_page, candidate)

        if candidate.stat().st_size > MAX_BYTES:
            if start_page == end_page:
                raise MineruError(f"{source} 的第 {start_page} 页单页超过 200MB，无法满足 MinerU 单文件限制。")
            midpoint = start_page + (page_count // 2) - 1
            if not use_original and candidate.exists():
                candidate.unlink()
            emit_range(start_page, midpoint)
            emit_range(midpoint + 1, end_page)
            return

        chunks.append(make_pdf_chunk(source, original_label, original_index, start_page, end_page, candidate))

    emit_range(1, total_pages)
    return chunks


def url_file_name(url: str, fallback: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = Path(urllib.parse.unquote(path)).name
    return name or fallback


def download_url(url: str, output: Path, timeout: int = 120) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "PDF-parse/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response, output.open("wb") as f:
        shutil.copyfileobj(response, f)
    return output


def create_chunks(inputs: list[str], out_dir: Path, args: argparse.Namespace) -> list[Chunk]:
    split_dir = out_dir / "_work" / "splits"
    remote_dir = out_dir / "_work" / "remote"
    chunks: list[Chunk] = []
    for original_index, item in enumerate(inputs, start=1):
        if is_url(item):
            name = url_file_name(item, f"remote-{original_index}.pdf")
            if is_pdf_name(name) and not args.no_download_url_pdfs:
                local_copy = remote_dir / f"{original_index:03d}-{name}"
                log(f"下载远程 PDF 以检查页数和拆分限制：{item}")
                download_url(item, local_copy)
                chunks.extend(split_pdf_by_limits(local_copy, name, original_index, split_dir))
            else:
                stem = safe_stem(name, f"remote-{original_index}")
                data_id = safe_data_id(f"{original_index:03d}-{stem}")
                chunks.append(
                    Chunk(
                        chunk_id=data_id,
                        original_label=item,
                        original_index=original_index,
                        source_kind="url",
                        file_name=name,
                        pages=1,
                        data_id=data_id,
                        is_pdf=is_pdf_name(name),
                        url=item,
                    )
                )
            continue

        path = Path(item).expanduser().resolve()
        if not path.exists():
            raise MineruError(f"找不到输入文件：{path}")
        if path.is_dir():
            raise MineruError(f"暂不接受目录作为输入，请展开为文件列表：{path}")
        if is_pdf_name(path.name):
            chunks.extend(split_pdf_by_limits(path, path.name, original_index, split_dir))
        else:
            if path.stat().st_size > MAX_BYTES:
                raise MineruError(f"{path} 超过 200MB，非 PDF 文件无法自动拆分页。")
            stem = safe_stem(path.name, f"input-{original_index}")
            data_id = safe_data_id(f"{original_index:03d}-{stem}")
            chunks.append(
                Chunk(
                    chunk_id=data_id,
                    original_label=path.name,
                    original_index=original_index,
                    source_kind="local",
                    file_name=path.name,
                    pages=1,
                    data_id=data_id,
                    is_pdf=False,
                    local_path=path,
                    upload_path=path,
                )
            )
    return chunks


def clone_pdf_range(chunk: Chunk, start_page: int, end_page: int) -> Chunk:
    if not chunk.local_path or not chunk.is_pdf:
        raise MineruError("只能拆分本地 PDF 分片。")
    return make_pdf_chunk(chunk.local_path, chunk.original_label, chunk.original_index, start_page, end_page, None)


def split_chunk_for_priority(chunk: Chunk, first_pages: int) -> tuple[Chunk, Chunk]:
    if not chunk.is_pdf or chunk.page_start is None or chunk.page_end is None:
        raise MineruError("只有 PDF 分片可以按高优先级额度边界继续拆分。")
    first_end = chunk.page_start + first_pages - 1
    first = clone_pdf_range(chunk, chunk.page_start, first_end)
    second = clone_pdf_range(chunk, first_end + 1, chunk.page_end)
    return first, second


def assign_chunks(chunks: list[Chunk], accounts: list[Account], state: dict[str, Any], day: dt.date) -> list[Assignment]:
    if not accounts:
        raise MineruError("没有可用账号：请检查 token 是否已过期或配置是否为空。")
    remaining = {
        account.name: max(0, account.daily_priority_pages - priority_used(state, day, account.name))
        for account in accounts
    }
    assignments: list[Assignment] = []
    slow_index = 0

    for chunk in chunks:
        queue = [chunk]
        while queue:
            current = queue.pop(0)
            assigned = False
            for account in accounts:
                available = remaining.get(account.name, 0)
                if available <= 0:
                    continue
                if current.pages <= available:
                    assignments.append(Assignment(current, account, "priority"))
                    remaining[account.name] = available - current.pages
                    assigned = True
                    break
                if current.is_pdf and current.pages > 1:
                    first_pages = min(available, current.pages)
                    if first_pages > 0:
                        first, second = split_chunk_for_priority(current, first_pages)
                        assignments.append(Assignment(first, account, "priority"))
                        remaining[account.name] = 0
                        queue.insert(0, second)
                        assigned = True
                        break
            if assigned:
                continue
            account = accounts[slow_index % len(accounts)]
            slow_index += 1
            assignments.append(Assignment(current, account, "slow"))

    return assignments


def materialize_chunk(chunk: Chunk, split_dir: Path) -> Path:
    if chunk.source_kind == "url":
        raise MineruError("URL 分片不需要本地物化。")
    if not chunk.local_path:
        raise MineruError(f"分片 {chunk.chunk_id} 缺少本地路径。")
    if not chunk.is_pdf:
        return chunk.local_path
    if chunk.upload_path and chunk.upload_path.exists():
        return chunk.upload_path
    if chunk.page_start is None or chunk.page_end is None:
        return chunk.local_path
    output = split_dir / chunk.file_name
    write_pdf_slice(chunk.local_path, chunk.page_start, chunk.page_end, output)
    if output.stat().st_size > MAX_BYTES:
        raise MineruError(f"拆分后的 {output.name} 仍超过 200MB，请手动检查源 PDF。")
    chunk.upload_path = output
    return output


def auth_headers(account: Account) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {account.token}",
        "Accept": "*/*",
    }


def http_json(method: str, url: str, payload: dict[str, Any] | None, account: Account, retries: int = 3) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = auth_headers(account)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
                result = json.loads(body)
                code = result.get("code")
                if code not in (0, "0", None):
                    raise MineruApiError(code, str(result.get("msg", "")), result.get("trace_id"))
                return result
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = MineruError(f"HTTP {exc.code}: {body}")
            if exc.code not in TRANSIENT_HTTP or attempt >= retries:
                break
        except MineruApiError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
        time.sleep(min(2**attempt, 10))
    raise MineruError(f"请求 MinerU 失败：{last_error}")


def put_file(url: str, path: Path, retries: int = 3) -> None:
    last_error: Exception | None = None
    payload = path.read_bytes()
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=payload, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                if 200 <= response.status < 300:
                    return
                last_error = MineruError(f"HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            last_error = MineruError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:300]}")
            if exc.code not in TRANSIENT_HTTP or attempt >= retries:
                break
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
        time.sleep(min(2**attempt, 10))
    raise MineruError(f"上传文件失败：{path}，原因：{last_error}")


def mineru_payload(defaults: Defaults, files: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "files": files,
        "model_version": defaults.model_version,
        "language": defaults.language,
        "enable_table": defaults.enable_table,
        "enable_formula": defaults.enable_formula,
    }
    if defaults.extra_formats:
        payload["extra_formats"] = defaults.extra_formats
    if defaults.no_cache:
        payload["no_cache"] = True
    if defaults.cache_tolerance is not None:
        payload["cache_tolerance"] = defaults.cache_tolerance
    return payload


def file_item_for_chunk(chunk: Chunk, defaults: Defaults, url_mode: bool) -> dict[str, Any]:
    item: dict[str, Any] = {"data_id": chunk.data_id}
    if url_mode:
        item["url"] = chunk.url
    else:
        item["name"] = chunk.file_name
    if defaults.is_ocr:
        item["is_ocr"] = True
    return item


def poll_batch(account: Account, batch_id: str, poll_interval: int, timeout: int) -> list[dict[str, Any]]:
    deadline = time.time() + timeout
    last_summary = ""
    while time.time() < deadline:
        result = http_json("GET", f"{BATCH_RESULT_ENDPOINT}/{batch_id}", None, account)
        data = result.get("data") or {}
        rows = data.get("extract_result") or []
        if not isinstance(rows, list):
            rows = []
        counts: dict[str, int] = {}
        for row in rows:
            state = str(row.get("state", "unknown"))
            counts[state] = counts.get(state, 0) + 1
        summary = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "等待结果"
        if summary != last_summary:
            log(f"批次 {batch_id} 状态：{summary}")
            last_summary = summary
        if rows and all(str(row.get("state")) in TERMINAL_STATES for row in rows):
            return rows
        time.sleep(poll_interval)
    raise MineruError(f"等待批次 {batch_id} 超时。")


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if not str(target).startswith(str(base)):
                raise MineruError(f"zip 中包含不安全路径：{member.filename}")
            zf.extract(member, destination)


def download_result_zip(url: str, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    download_url(url, output, timeout=180)
    return output


def result_key(row: dict[str, Any]) -> str:
    return str(row.get("data_id") or row.get("file_name") or "")


def process_batch(
    job: BatchJob,
    defaults: Defaults,
    out_dir: Path,
    args: argparse.Namespace,
    state: dict[str, Any],
    state_lock: threading.Lock,
    day: dt.date,
) -> None:
    account = job.account
    mode_label = "高优先级" if job.mode == "priority" else "慢速"
    log(f"提交批次：账号={account.name}，模式={mode_label}，类型={job.source_kind}，文件数={len(job.assignments)}")

    if job.source_kind == "local":
        split_dir = out_dir / "_work" / "splits"
        for assignment in job.assignments:
            materialize_chunk(assignment.chunk, split_dir)
        files = [file_item_for_chunk(a.chunk, defaults, url_mode=False) for a in job.assignments]
        response = http_json("POST", LOCAL_BATCH_ENDPOINT, mineru_payload(defaults, files), account)
        data = response.get("data") or {}
        batch_id = data.get("batch_id")
        upload_urls = data.get("file_urls") or []
        if not batch_id or len(upload_urls) != len(job.assignments):
            raise MineruError(f"申请上传 URL 返回异常：batch_id={batch_id}, file_urls={len(upload_urls)}")
        for assignment, upload_url in zip(job.assignments, upload_urls):
            path = materialize_chunk(assignment.chunk, split_dir)
            put_file(str(upload_url), path)
        with state_lock:
            for assignment in job.assignments:
                add_usage(state, day, assignment.account.name, assignment.mode, assignment.chunk.pages)
            save_state(Path(args.state_file), state)
    else:
        files = [file_item_for_chunk(a.chunk, defaults, url_mode=True) for a in job.assignments]
        response = http_json("POST", URL_BATCH_ENDPOINT, mineru_payload(defaults, files), account)
        batch_id = (response.get("data") or {}).get("batch_id")
        if not batch_id:
            raise MineruError("URL 批量提交没有返回 batch_id。")
        with state_lock:
            for assignment in job.assignments:
                add_usage(state, day, assignment.account.name, assignment.mode, assignment.chunk.pages)
            save_state(Path(args.state_file), state)

    rows = poll_batch(account, str(batch_id), args.poll_interval, args.timeout)
    by_key = {result_key(row): row for row in rows if result_key(row)}
    zip_dir = out_dir / "_work" / "zips"
    parts_dir = out_dir / "parts"
    for assignment in job.assignments:
        row = by_key.get(assignment.chunk.data_id) or by_key.get(assignment.chunk.file_name)
        if not row:
            assignment.state = "failed"
            assignment.error = "批量结果中找不到该文件。"
            continue
        assignment.state = str(row.get("state", "unknown"))
        if assignment.state == "failed":
            assignment.error = str(row.get("err_msg") or "MinerU 返回 failed。")
            continue
        zip_url = row.get("full_zip_url")
        if not zip_url:
            assignment.state = "failed"
            assignment.error = "MinerU 完成但未返回 full_zip_url。"
            continue
        zip_path = zip_dir / f"{assignment.chunk.chunk_id}.zip"
        result_dir = parts_dir / assignment.chunk.chunk_id
        download_result_zip(str(zip_url), zip_path)
        safe_extract_zip(zip_path, result_dir)
        assignment.zip_path = zip_path
        assignment.result_dir = result_dir
        assignment.state = "done"


def build_batches(assignments: list[Assignment]) -> list[BatchJob]:
    grouped: dict[tuple[str, str, str], list[Assignment]] = {}
    account_lookup: dict[str, Account] = {}
    for assignment in assignments:
        key = (assignment.account.name, assignment.mode, assignment.chunk.source_kind)
        grouped.setdefault(key, []).append(assignment)
        account_lookup[assignment.account.name] = assignment.account

    jobs: list[BatchJob] = []
    for (account_name, mode, source_kind), items in grouped.items():
        account = account_lookup[account_name]
        for index in range(0, len(items), BATCH_LIMIT):
            jobs.append(BatchJob(account, mode, source_kind, items[index : index + BATCH_LIMIT]))
    return jobs


def run_jobs(
    jobs: list[BatchJob],
    defaults: Defaults,
    out_dir: Path,
    args: argparse.Namespace,
    state: dict[str, Any],
    day: dt.date,
) -> None:
    semaphores: dict[tuple[str, str], threading.Semaphore] = {}
    max_workers = 0
    for job in jobs:
        limit = job.account.priority_concurrency if job.mode == "priority" else job.account.slow_concurrency
        semaphores[(job.account.name, job.mode)] = threading.Semaphore(limit)
        max_workers += limit
    if args.concurrency:
        max_workers = min(max_workers or args.concurrency, args.concurrency)
    max_workers = max(1, max_workers)
    state_lock = threading.Lock()

    def wrapped(job: BatchJob) -> None:
        semaphore = semaphores[(job.account.name, job.mode)]
        with semaphore:
            process_batch(job, defaults, out_dir, args, state, state_lock, day)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(wrapped, job): job for job in jobs}
        for future in concurrent.futures.as_completed(future_map):
            job = future_map[future]
            try:
                future.result()
            except Exception as exc:
                for assignment in job.assignments:
                    assignment.state = "failed"
                    assignment.error = str(exc)
                log(f"批次失败：账号={job.account.name}，原因={exc}")


def find_first(root: Path, predicate) -> Path | None:
    matches = sorted([p for p in root.rglob("*") if p.is_file() and predicate(p)], key=lambda p: str(p))
    return matches[0] if matches else None


def find_all(root: Path, predicate) -> list[Path]:
    return sorted([p for p in root.rglob("*") if p.is_file() and predicate(p)], key=lambda p: str(p))


def is_relative_asset(path: str) -> bool:
    value = path.strip().strip("'\"")
    if not value or value.startswith(("#", "data:", "mailto:", "javascript:")):
        return False
    parsed = urllib.parse.urlparse(value)
    return not parsed.scheme and not value.startswith("/")


def prefix_asset_path(path: str, prefix: str) -> str:
    if not is_relative_asset(path):
        return path
    return f"{prefix}/{path.lstrip('./')}"


def rewrite_markdown_assets(text: str, prefix: str) -> str:
    def repl(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2)
        return f"![{label}]({prefix_asset_path(target, prefix)})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, text)


def rewrite_html_assets(text: str, prefix: str) -> str:
    def repl(match: re.Match[str]) -> str:
        attr = match.group(1)
        quote = match.group(2)
        target = match.group(3)
        return f"{attr}={quote}{html.escape(prefix_asset_path(target, prefix), quote=True)}{quote}"

    return re.sub(r"\b(src|href)=(['\"])([^'\"]+)\2", repl, text, flags=re.IGNORECASE)


def copy_part_assets(part_dir: Path, assets_dir: Path, part_name: str) -> str:
    target = assets_dir / part_name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(part_dir, target)
    return f"assets/{part_name}"


def recursive_adjust_page_idx(value: Any, offset: int) -> Any:
    if isinstance(value, dict):
        adjusted = {}
        for key, item in value.items():
            if key == "page_idx" and isinstance(item, int):
                adjusted[key] = item + offset
            else:
                adjusted[key] = recursive_adjust_page_idx(item, offset)
        return adjusted
    if isinstance(value, list):
        return [recursive_adjust_page_idx(item, offset) for item in value]
    return value


def json_kind(path: Path) -> str | None:
    name = path.name.lower()
    if re.search(r"(^|_)content_list_v2\.json$", name):
        return "content_list_v2"
    if re.search(r"(^|_)content_list\.json$", name):
        return "content_list"
    if re.search(r"(^|_)middle\.json$", name):
        return "middle"
    if re.search(r"(^|_)model\.json$", name):
        return "model"
    return None


def merge_json_kind(kind: str, inputs: list[tuple[Path, int]], output: Path) -> None:
    if not inputs:
        return
    merged: Any
    if kind == "middle":
        merged = None
        pdf_info: list[Any] = []
        fallback_items: list[Any] = []
        for path, offset in inputs:
            data = json.loads(path.read_text(encoding="utf-8"))
            adjusted = recursive_adjust_page_idx(data, offset)
            if isinstance(adjusted, dict) and isinstance(adjusted.get("pdf_info"), list):
                if merged is None:
                    merged = {k: v for k, v in adjusted.items() if k != "pdf_info"}
                pdf_info.extend(adjusted["pdf_info"])
            else:
                fallback_items.append(adjusted)
        if merged is not None:
            merged["pdf_info"] = pdf_info
        else:
            merged = fallback_items
    else:
        merged_list: list[Any] = []
        for path, offset in inputs:
            data = json.loads(path.read_text(encoding="utf-8"))
            adjusted = recursive_adjust_page_idx(data, offset)
            if isinstance(adjusted, list):
                merged_list.extend(adjusted)
            else:
                merged_list.append(adjusted)
        merged = merged_list
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_html_body(text: str) -> str:
    match = re.search(r"<body[^>]*>(.*?)</body>", text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else text


def merge_pdfs(inputs: list[Path], output: Path) -> None:
    if not inputs:
        return
    require_pypdf()
    writer = PdfWriter()
    for path in inputs:
        reader = PdfReader(str(path))
        for page in reader.pages:
            writer.add_page(page)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as f:
        writer.write(f)


def successful_assignments(assignments: list[Assignment]) -> list[Assignment]:
    return [a for a in assignments if a.state == "done" and a.result_dir]


def merge_results(assignments: list[Assignment], out_dir: Path) -> Path:
    merged_dir = out_dir / "merged"
    assets_dir = merged_dir / "assets"
    if merged_dir.exists():
        shutil.rmtree(merged_dir)
    merged_dir.mkdir(parents=True, exist_ok=True)

    done = sorted(successful_assignments(assignments), key=lambda a: (a.chunk.original_index, a.chunk.page_start or 0, a.chunk.chunk_id))
    markdown_parts: list[str] = []
    html_parts: list[str] = []
    json_inputs: dict[str, list[tuple[Path, int]]] = {"model": [], "middle": [], "content_list": [], "content_list_v2": []}
    layout_pdfs: list[Path] = []
    span_pdfs: list[Path] = []
    docx_parts: list[Path] = []
    latex_parts: list[Path] = []
    cumulative_offset = 0

    manifest_parts: list[dict[str, Any]] = []
    for index, assignment in enumerate(done, start=1):
        part_dir = assignment.result_dir
        assert part_dir is not None
        part_name = f"part{index:03d}-{assignment.chunk.chunk_id}"
        asset_prefix = copy_part_assets(part_dir, assets_dir, part_name)

        md_file = find_first(part_dir, lambda p: p.name.lower() == "full.md" or p.suffix.lower() == ".md")
        if md_file:
            text = md_file.read_text(encoding="utf-8", errors="replace")
            markdown_parts.append(
                f"\n\n<!-- PDF-parse {part_name}: {assignment.chunk.original_label} pages {assignment.chunk.page_start or '-'}-{assignment.chunk.page_end or '-'} -->\n\n"
                + rewrite_markdown_assets(text, asset_prefix)
            )

        html_file = find_first(part_dir, lambda p: p.suffix.lower() == ".html")
        if html_file:
            raw_html = html_file.read_text(encoding="utf-8", errors="replace")
            body = rewrite_html_assets(extract_html_body(raw_html), asset_prefix)
            html_parts.append(f"<section data-pdf-parse-part=\"{html.escape(part_name)}\">{body}</section>")

        for json_file in find_all(part_dir, lambda p: p.suffix.lower() == ".json"):
            kind = json_kind(json_file)
            if kind:
                json_inputs[kind].append((json_file, cumulative_offset))

        layout_pdfs.extend(find_all(part_dir, lambda p: p.name.lower().endswith("layout.pdf")))
        span_pdfs.extend(find_all(part_dir, lambda p: p.name.lower().endswith("span.pdf")))
        docx_parts.extend(find_all(part_dir, lambda p: p.suffix.lower() == ".docx"))
        latex_parts.extend(find_all(part_dir, lambda p: p.suffix.lower() in {".tex", ".latex"}))

        manifest_parts.append(
            {
                "part": part_name,
                "source": assignment.chunk.original_label,
                "pages": assignment.chunk.pages,
                "page_start": assignment.chunk.page_start,
                "page_end": assignment.chunk.page_end,
                "account": assignment.account.name,
                "mode": assignment.mode,
            }
        )
        cumulative_offset += max(assignment.chunk.pages, 1)

    if markdown_parts:
        (merged_dir / "full.md").write_text("".join(markdown_parts).lstrip(), encoding="utf-8")

    for kind, items in json_inputs.items():
        if items:
            output_name = {
                "model": "model.json",
                "middle": "middle.json",
                "content_list": "content_list.json",
                "content_list_v2": "content_list_v2.json",
            }[kind]
            merge_json_kind(kind, items, merged_dir / output_name)

    if layout_pdfs:
        merge_pdfs(layout_pdfs, merged_dir / "layout.pdf")
    if span_pdfs:
        merge_pdfs(span_pdfs, merged_dir / "span.pdf")

    if html_parts:
        html_doc = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>PDF-parse merged</title></head>"
            "<body>"
            + "\n".join(html_parts)
            + "</body></html>"
        )
        (merged_dir / "index.html").write_text(html_doc, encoding="utf-8")

    if docx_parts:
        docx_dir = merged_dir / "docx_parts"
        docx_dir.mkdir(exist_ok=True)
        for index, path in enumerate(docx_parts, start=1):
            shutil.copy2(path, docx_dir / f"part{index:03d}-{path.name}")

    if latex_parts:
        latex_dir = merged_dir / "latex_parts"
        latex_dir.mkdir(exist_ok=True)
        for index, path in enumerate(latex_parts, start=1):
            shutil.copy2(path, latex_dir / f"part{index:03d}-{path.name}")

    failed = [
        {
            "chunk_id": a.chunk.chunk_id,
            "source": a.chunk.original_label,
            "state": a.state,
            "error": a.error,
            "account": a.account.name,
            "mode": a.mode,
        }
        for a in assignments
        if a.state != "done"
    ]
    manifest = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "parts": manifest_parts,
        "failed": failed,
        "notes": [
            "DOCX 和 LaTeX 分片默认保留在 docx_parts/ 与 latex_parts/，因为跨分片结构化合并可能破坏格式。",
            "JSON 合并已按分片顺序调整 page_idx。",
        ],
    }
    (merged_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest_md = ["# PDF-parse 合并报告", "", f"- 成功分片：{len(done)}", f"- 失败分片：{len(failed)}"]
    if docx_parts or latex_parts:
        manifest_md.append("- DOCX/LaTeX 已按分片保留，未做强制结构化合并。")
    if failed:
        manifest_md.append("")
        manifest_md.append("## 失败分片")
        for item in failed:
            manifest_md.append(f"- {item['chunk_id']}：{item['error']}")
    (merged_dir / "manifest.md").write_text("\n".join(manifest_md) + "\n", encoding="utf-8")
    return merged_dir


def print_plan(assignments: list[Assignment], warnings: list[str], out_dir: Path) -> None:
    print(FIRST_USE_GUIDE)
    if warnings:
        log("账号提醒：")
        for warning in warnings:
            log(f"- {warning}")
    log("解析计划：")
    for index, assignment in enumerate(assignments, start=1):
        chunk = assignment.chunk
        page_text = f"{chunk.pages}页"
        if chunk.page_start is not None and chunk.page_end is not None:
            page_text += f"（源页 {chunk.page_start}-{chunk.page_end}）"
        mode_text = "高优先级" if assignment.mode == "priority" else "慢速"
        log(
            f"{index:03d}. {chunk.file_name} | {page_text} | 账号 {assignment.account.name} "
            f"({mask_token(assignment.account.token)}) | {mode_text}"
        )
    priority_pages = sum(a.chunk.pages for a in assignments if a.mode == "priority")
    slow_pages = sum(a.chunk.pages for a in assignments if a.mode == "slow")
    log(f"高优先级页数：{priority_pages}；慢速页数：{slow_pages}")
    log(f"输出目录：{out_dir}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MinerU VLM 中文精准批量解析 CLI")
    parser.add_argument("inputs", nargs="*", help="本地文件路径或 URL")
    parser.add_argument("--accounts", help="MinerU 多账号 YAML 配置")
    parser.add_argument("--out-dir", default="mineru-output", help="输出目录")
    parser.add_argument("--state-file", default="mineru_state.json", help="本地额度状态 JSON")
    parser.add_argument("--dry-run", action="store_true", help="只展示计划，不调用 MinerU")
    parser.add_argument("--print-config-template", action="store_true", help="输出 YAML 配置模板")
    parser.add_argument("--language", help="文档语言，默认从 YAML 读取，通常为 ch")
    parser.add_argument("--ocr", action="store_true", help="开启 OCR")
    parser.add_argument("--no-table", action="store_true", help="关闭表格识别")
    parser.add_argument("--no-formula", action="store_true", help="关闭公式识别")
    parser.add_argument("--extra-formats", help="额外格式，逗号分隔，如 docx,html,latex")
    parser.add_argument("--no-cache", action="store_true", help="请求 MinerU 绕过 URL 缓存")
    parser.add_argument("--cache-tolerance", type=int, help="URL 缓存容忍秒数")
    parser.add_argument("--poll-interval", type=int, default=10, help="轮询间隔秒数")
    parser.add_argument("--timeout", type=int, default=7200, help="单批次超时秒数")
    parser.add_argument("--concurrency", type=int, help="全局最大批次并发上限")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="每日额度重置时区")
    parser.add_argument("--no-download-url-pdfs", action="store_true", help="URL PDF 不预下载检查页数，直接交给 MinerU")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.print_config_template:
        print(CONFIG_TEMPLATE, end="")
        return 0
    if not args.inputs:
        print(FIRST_USE_GUIDE)
        log("请提供输入文件或 URL；如还没有配置，可运行：")
        log("python pdf-parse/scripts/mineru_parse.py --print-config-template > mineru_accounts.yaml")
        return 2
    if not args.accounts:
        raise MineruError("请通过 --accounts 指定 MinerU API token YAML 配置。")

    accounts, defaults = load_config(Path(args.accounts))
    defaults = apply_arg_overrides(defaults, args)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    day = today_in_timezone(args.timezone)
    active_accounts, warnings = account_expiry_warnings(accounts, day)
    state = load_state(Path(args.state_file))
    chunks = create_chunks(args.inputs, out_dir, args)
    assignments = assign_chunks(chunks, active_accounts, state, day)
    print_plan(assignments, warnings, out_dir)

    if args.dry_run:
        log("dry-run 完成：没有调用 MinerU，也没有更新额度状态。")
        return 0

    jobs = build_batches(assignments)
    run_jobs(jobs, defaults, out_dir, args, state, day)
    merged_dir = merge_results(assignments, out_dir)
    failures = [a for a in assignments if a.state != "done"]
    if failures:
        log(f"完成但有 {len(failures)} 个分片失败，请查看 {merged_dir / 'manifest.md'}。")
        return 1
    log(f"解析完成，合并结果在：{merged_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("用户中断。")
        raise SystemExit(130)
    except MineruError as exc:
        log(f"错误：{exc}")
        raise SystemExit(1)
