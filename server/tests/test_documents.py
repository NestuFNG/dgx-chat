"""Attachment document parsing: PDF (pypdf), DOCX (python-docx), graceful errors.

XLSX is intentionally unsupported because openpyxl is not a dependency.
"""

from __future__ import annotations

import io

import pytest

from tests.conftest import text_script

OFFICIAL_SYSTEM = "该助手为DeepSeek，由DeepSeek创造。"


def make_pdf(text: str, *, with_text: bool = True) -> bytes:
    stream = (
        f"BT /F1 14 Tf 30 120 Td ({text}) Tj ET".encode() if with_text else b"q Q"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph("Hello DOCX World")
    document.add_paragraph("Second paragraph")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "CellA"
    table.cell(0, 1).text = "CellB"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def upload(harness, token: str, name: str, data: bytes, mime: str) -> str:
    response = harness.client.post(
        "/anthropic/v1/files",
        headers={"x-api-key": token},
        files={"file": (name, data, mime)},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def document_body(file_id: str, question: str = "总结一下这个文档") -> dict:
    return {
        "model": "deepseek-flash",
        "system": OFFICIAL_SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "document", "source": {"type": "file", "file_id": file_id}},
                ],
            }
        ],
        "thinking": {"type": "disabled"},
        "safety_check": {"level": "high"},
        "tools": [],
    }


def last_user_content(harness) -> str:
    payload = harness.captured["payloads"][0]
    content = payload["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(
        part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
    )


def test_pdf_is_extracted_and_inlined(admin_harness):
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "report.pdf",
        make_pdf("Hello PDF World"), "application/pdf",
    )
    status, _, headers = admin_harness.stream_message(
        admin_harness.admin_token, document_body(file_id)
    )
    assert status == 200, headers
    content = last_user_content(admin_harness)
    assert '<document name="report.pdf">' in content
    assert "Hello PDF World" in content


def test_docx_is_extracted_with_tables(admin_harness):
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "notes.docx", make_docx(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    status, _, _ = admin_harness.stream_message(
        admin_harness.admin_token, document_body(file_id)
    )
    assert status == 200
    content = last_user_content(admin_harness)
    assert "Hello DOCX World" in content
    assert "Second paragraph" in content
    assert "CellA" in content and "CellB" in content


def test_corrupt_pdf_returns_clear_400(admin_harness):
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "broken.pdf",
        b"%PDF-1.4 this is not really a pdf", "application/pdf",
    )
    response = admin_harness.client.post(
        "/anthropic/v1/messages",
        headers={"x-api-key": admin_harness.admin_token, "content-type": "application/json"},
        json=document_body(file_id),
    )
    assert response.status_code == 400
    body = response.json()
    assert "PDF" in body["error"]["message"]
    assert body["error"]["type"] == "invalid_request_error"


def test_pdf_without_text_is_a_clear_error(admin_harness):
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "scan.pdf",
        make_pdf("", with_text=False), "application/pdf",
    )
    response = admin_harness.client.post(
        "/anthropic/v1/messages",
        headers={"x-api-key": admin_harness.admin_token, "content-type": "application/json"},
        json=document_body(file_id),
    )
    assert response.status_code == 400
    assert "没有可提取的文本" in response.json()["error"]["message"]


def test_xlsx_is_skipped_with_a_notice(admin_harness):
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "sheet.xlsx", b"PK\x03\x04fake",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    status, _, headers = admin_harness.stream_message(
        admin_harness.admin_token, document_body(file_id)
    )
    assert status == 200
    assert "xlsx_not_supported" in headers.get("x-dgx-notice", "")
    content = last_user_content(admin_harness)
    assert "<document" not in content


def test_document_char_budget_truncates(admin_harness):
    admin_harness.app.state.settings.max_document_chars = 100
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "long.txt",
        ("line of text " * 100).encode(), "text/plain",
    )
    status, _, headers = admin_harness.stream_message(
        admin_harness.admin_token, document_body(file_id)
    )
    assert status == 200
    content = last_user_content(admin_harness)
    assert "[truncated]" in content
    assert "document_truncated" in headers.get("x-dgx-notice", "")
    # 50 chars + wrapper/marker, far below the original ~1300 chars
    assert len(content) < 300


def test_document_size_limit_is_enforced(admin_harness):
    admin_harness.app.state.settings.max_document_bytes = 1024
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "big.txt", b"x" * 4096, "text/plain",
    )
    response = admin_harness.client.post(
        "/anthropic/v1/messages",
        headers={"x-api-key": admin_harness.admin_token, "content-type": "application/json"},
        json=document_body(file_id),
    )
    assert response.status_code == 413


def test_plain_text_document_behaviour_is_unchanged(admin_harness):
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "plain.md", "# 标题\n正文内容".encode(),
        "text/markdown",
    )
    status, _, _ = admin_harness.stream_message(
        admin_harness.admin_token, document_body(file_id)
    )
    assert status == 200
    content = last_user_content(admin_harness)
    assert '<document name="plain.md">' in content
    assert "# 标题\n正文内容" in content


def test_binary_attachment_is_still_skipped(admin_harness):
    admin_harness.fake_upstream(text_script("好的"))
    file_id = upload(
        admin_harness, admin_harness.admin_token, "blob.bin", b"\x00\x01\x02", 
        "application/octet-stream",
    )
    status, _, headers = admin_harness.stream_message(
        admin_harness.admin_token, document_body(file_id)
    )
    assert status == 200
    assert "unsupported_document_skipped" in headers.get("x-dgx-notice", "")
