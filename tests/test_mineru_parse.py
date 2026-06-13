import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from pypdf import PdfWriter


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mineru_parse.py"
spec = importlib.util.spec_from_file_location("mineru_parse", SCRIPT)
mineru_parse = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["mineru_parse"] = mineru_parse
spec.loader.exec_module(mineru_parse)


def make_pdf(path: Path, pages: int) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as f:
        writer.write(f)


def test_split_201_pages(tmp: Path) -> None:
    pdf = tmp / "long.pdf"
    make_pdf(pdf, 201)
    chunks = mineru_parse.split_pdf_by_limits(pdf, "long.pdf", 1, tmp / "splits")
    assert [c.pages for c in chunks] == [200, 1]
    assert all(c.upload_path and c.upload_path.exists() for c in chunks)


def test_priority_quota_boundary_split() -> None:
    day = date(2026, 6, 13)
    state = {"days": {day.isoformat(): {"accounts": {"a": {"priority_pages_used": 800, "slow_pages_used": 0}}}}}
    accounts = [
        mineru_parse.Account("a", "token-a", "2026-12-31", daily_priority_pages=1000),
        mineru_parse.Account("b", "token-b", "2026-12-31", daily_priority_pages=1000),
    ]
    chunks = [
        mineru_parse.Chunk(
            chunk_id="c1",
            original_label="x.pdf",
            original_index=1,
            source_kind="local",
            file_name="x_1.pdf",
            pages=150,
            data_id="c1",
            is_pdf=True,
            local_path=Path("x.pdf"),
            page_start=1,
            page_end=150,
        ),
        mineru_parse.Chunk(
            chunk_id="c2",
            original_label="x.pdf",
            original_index=1,
            source_kind="local",
            file_name="x_2.pdf",
            pages=150,
            data_id="c2",
            is_pdf=True,
            local_path=Path("x.pdf"),
            page_start=151,
            page_end=300,
        ),
    ]
    assignments = mineru_parse.assign_chunks(chunks, accounts, state, day)
    assert [(a.account.name, a.mode, a.chunk.pages) for a in assignments] == [
        ("a", "priority", 150),
        ("a", "priority", 50),
        ("b", "priority", 100),
    ]


def test_expiry_warnings() -> None:
    accounts = [
        mineru_parse.Account("expired", "t", "2026-06-01"),
        mineru_parse.Account("soon", "t", "2026-06-20"),
        mineru_parse.Account("missing", "t", None),
    ]
    active, warnings = mineru_parse.account_expiry_warnings(accounts, date(2026, 6, 13))
    assert [a.name for a in active] == ["soon", "missing"]
    text = "\n".join(warnings)
    assert "已跳过" in text
    assert "还剩 7 天" in text
    assert "未填写 api_expires_at" in text


def test_merge_json_and_markdown(tmp: Path) -> None:
    assignments = []
    for idx, pages in enumerate([2, 3], start=1):
        part = tmp / "out" / "parts" / f"part{idx}"
        (part / "images").mkdir(parents=True)
        (part / "images" / "a.png").write_bytes(b"png")
        (part / "full.md").write_text(f"![x](images/a.png)\npart {idx}", encoding="utf-8")
        (part / f"demo_content_list.json").write_text(json.dumps([{"page_idx": 0, "text": idx}]), encoding="utf-8")
        (part / f"demo_model.json").write_text(json.dumps([[{"type": "text"}]]), encoding="utf-8")
        (part / f"demo_middle.json").write_text(json.dumps({"_backend": "vlm", "pdf_info": [{"page_idx": 0}]}), encoding="utf-8")
        chunk = mineru_parse.Chunk(
            chunk_id=f"c{idx}",
            original_label="demo.pdf",
            original_index=1,
            source_kind="local",
            file_name=f"demo_{idx}.pdf",
            pages=pages,
            data_id=f"c{idx}",
            is_pdf=True,
            page_start=1 if idx == 1 else 3,
            page_end=2 if idx == 1 else 5,
        )
        assignment = mineru_parse.Assignment(chunk, mineru_parse.Account("a", "tok"), "priority", result_dir=part)
        assignment.state = "done"
        assignments.append(assignment)
    merged = mineru_parse.merge_results(assignments, tmp / "out")
    md = (merged / "full.md").read_text(encoding="utf-8")
    assert "assets/part001-c1/images/a.png" in md
    content = json.loads((merged / "content_list.json").read_text(encoding="utf-8"))
    assert [item["page_idx"] for item in content] == [0, 2]
    middle = json.loads((merged / "middle.json").read_text(encoding="utf-8"))
    assert [item["page_idx"] for item in middle["pdf_info"]] == [0, 2]


def test_cli_dry_run(tmp: Path) -> None:
    pdf = tmp / "tiny.pdf"
    make_pdf(pdf, 3)
    accounts = tmp / "accounts.yaml"
    accounts.write_text(
        """
mineru_accounts:
  - name: a
    token: "TOKEN1234567890"
    api_expires_at: "2026-12-31"
defaults:
  model_version: "vlm"
  language: "ch"
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(pdf),
            "--accounts",
            str(accounts),
            "--out-dir",
            str(tmp / "out"),
            "--state-file",
            str(tmp / "state.json"),
            "--dry-run",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert "PDF-parse 使用提示" in result.stdout
    assert "高优先级页数：3" in result.stdout
    assert "dry-run 完成" in result.stdout


def main() -> int:
    tests = [
        test_split_201_pages,
        test_priority_quota_boundary_split,
        test_expiry_warnings,
        test_merge_json_and_markdown,
        test_cli_dry_run,
    ]
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        for test in tests:
            test_tmp = base / test.__name__
            test_tmp.mkdir()
            try:
                if test.__code__.co_argcount:
                    test(test_tmp)
                else:
                    test()
            except Exception as exc:
                print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
                raise
            print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
