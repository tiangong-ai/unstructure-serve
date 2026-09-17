from pathlib import Path

import pytest

from src.services import pdf_text_layer_reconcile as reconcile


def _bbox_xml(words_by_page: list[list[tuple[float, float, str]]]) -> str:
    pages = []
    for words in words_by_page:
        word_xml = "\n".join(
            f'<word xMin="{x}" yMin="{y}" xMax="{x + 10}" yMax="{y + 10}">{text}</word>'
            for x, y, text in words
        )
        pages.append(f'<page width="595" height="842">{word_xml}</page>')
    return f"<html><body><doc>{''.join(pages)}</doc></body></html>"


def _pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    return path


def test_reconcile_content_list_checkboxes_repairs_table_rows(monkeypatch, tmp_path):
    content_list = [
        {
            "type": "table",
            "page_idx": 0,
            "table_body": (
                "<table>"
                "<tr><td>创新分类</td><td>☐基础前沿 ☑重大共性关键技术 ☐应用示范研究 ☐其他</td></tr>"
                "<tr><td>项目遴选方式</td><td>☐公开竞争 ☐定向委托 ☐定向择优</td></tr>"
                "<tr><td>项目实施模式</td><td>☐常规项目 ☐青年科学家 ☐揭榜挂帅 ☐滚动支持 "
                "☐赛马争先 ☐应急攻关 ☐其他模式(珍珠项目, 总承制项目)</td></tr>"
                "</table>"
            ),
        }
    ]
    bbox = _bbox_xml(
        [
            [
                (50, 10, "创新分类"),
                (140, 10, "□基础前沿"),
                (195, 10, "■重大共性关键技术"),
                (290, 10, "□应用示范研究"),
                (365, 10, "□其他"),
                (50, 30, "项目遴选方式"),
                (140, 30, "■公开竞争"),
                (195, 30, "□定向委托"),
                (250, 30, "□定向择优"),
                (50, 50, "项目实施模式"),
                (140, 50, "□常规项目"),
                (195, 50, "□青年科学家"),
                (260, 50, "□揭榜挂帅"),
                (315, 50, "□滚动支持"),
                (370, 50, "□赛马争先"),
                (425, 50, "□应急攻关"),
                (480, 50, "■其他模式(珍珠项"),
            ]
        ]
    )
    monkeypatch.setattr(reconcile, "_run_pdftotext_bbox", lambda _path: bbox)

    changes = reconcile.reconcile_content_list_checkboxes(content_list, _pdf_path(tmp_path))

    assert changes == 2
    table_body = content_list[0]["table_body"]
    assert "项目遴选方式</td><td>☑公开竞争 ☐定向委托 ☐定向择优" in table_body
    assert "☑其他模式(珍珠项目, 总承制项目)" in table_body
    assert "☐其他</td></tr>" in table_body


def test_reconcile_content_list_checkboxes_handles_short_option_cells(monkeypatch, tmp_path):
    content_list = [
        {
            "type": "table",
            "page_idx": 0,
            "table_body": (
                "<table><tr><td>项目负责人</td><td>姓名</td><td>刘睿</td>"
                "<td>性别</td><td>☐男 ☐女</td><td>出生日期</td><td>1982-09-12</td></tr>"
                "<tr><td>职称</td><td>☐正高级 ☐副高级 ☐中级 ☐初级 ☐其他</td>"
                "<td>职务</td><td>无</td></tr></table>"
            ),
        }
    ]
    bbox = _bbox_xml(
        [
            [
                (60, 10, "姓名"),
                (140, 10, "刘睿"),
                (250, 10, "性别"),
                (340, 10, "□男"),
                (365, 10, "■女"),
                (400, 10, "出生日期"),
                (140, 30, "□正高级"),
                (185, 30, "■副高级"),
                (230, 30, "□中级"),
                (265, 30, "□初级"),
                (300, 30, "□其他"),
            ]
        ]
    )
    monkeypatch.setattr(reconcile, "_run_pdftotext_bbox", lambda _path: bbox)

    changes = reconcile.reconcile_content_list_checkboxes(content_list, _pdf_path(tmp_path))

    assert changes == 2
    table_body = content_list[0]["table_body"]
    assert "☐男 ☑女" in table_body
    assert "☐正高级 ☑副高级 ☐中级" in table_body
    assert "出生日期" in table_body


def test_reconcile_content_list_checkboxes_can_be_disabled(monkeypatch, tmp_path):
    content_list = [
        {
            "type": "table",
            "page_idx": 0,
            "table_body": "<table><tr><td>项目遴选方式</td><td>☐公开竞争</td></tr></table>",
        }
    ]
    monkeypatch.setattr(
        reconcile,
        "_run_pdftotext_bbox",
        lambda _path: _bbox_xml([[(50, 10, "项目遴选方式"), (140, 10, "■公开竞争")]]),
    )
    monkeypatch.setenv("MINERU_TEXT_LAYER_CHECKBOX_RECONCILE", "false")

    changes = reconcile.reconcile_content_list_checkboxes(content_list, _pdf_path(tmp_path))

    assert changes == 0
    assert "☐公开竞争" in content_list[0]["table_body"]


@pytest.mark.parametrize(
    "cell,source_pages,expected_changes",
    [
        ("公开竞争 □定向委托 □定向择优", [["■公开竞争 □定向委托 □定向择优"]], 1),
        ("☑公开竞争 □定向委托 □定向择优", [["■公开竞争 □定向委托 □定向择优"]], 0),
        ("公开竞争 □定向委托 □定向择优", [["■公开竞争 □定向委托 □定向择优"]] * 2, 1),
        ("公开竞争 □定向委托 □定向择优", [[], ["■公开竞争 □定向委托 □定向择优"]], 0),
        (
            "公开竞争 □定向委托 □定向择优",
            [["■公开竞争 □定向委托 □定向择优", "□公开竞争 □定向委托 □定向择优"]],
            0,
        ),
        ("说明：公开竞争 □定向委托 □定向择优", [["■公开竞争 □定向委托 □定向择优"]], 0),
        ("公开竞争 □定向委托", [["■公开竞争 □定向委托"]], 0),
        ("其他 □选项甲 □选项乙", [["■其他 □选项甲 □选项乙"]], 0),
    ],
)
def test_missing_leading_marker_requires_unique_full_option_group(
    monkeypatch, tmp_path, cell, source_pages, expected_changes
):
    """Basic-tier p2 regression: restore a dropped glyph only with strong evidence."""
    content = [
        {"type": "table", "page_idx": 0, "table_body": f"<table><tr><td>{cell}</td></tr></table>"}
    ]
    bbox = _bbox_xml(
        [
            [
                (50 + column * 100, 10 + row * 30, word)
                for row, text in enumerate(rows)
                for column, word in enumerate(text.split())
            ]
            for rows in source_pages
        ]
    )
    monkeypatch.setattr(reconcile, "_run_pdftotext_bbox", lambda _path: bbox)

    changes = reconcile.reconcile_content_list_checkboxes(content, _pdf_path(tmp_path))

    assert changes == expected_changes
    expected = f"☑{cell}" if expected_changes else cell
    assert content[0]["table_body"] == f"<table><tr><td>{expected}</td></tr></table>"
