from service.convert import PAGE_SCHEMA, build_page_markdown


def test_page_schema_remains_five() -> None:
    assert PAGE_SCHEMA == 5


def test_build_page_markdown_preserves_file_marker() -> None:
    markdown = build_page_markdown(
        page_number=3,
        file_name="drawing.pdf",
        kind="drawing",
        pass_a="Описание",
    )
    assert "**Файл:** `drawing.pdf`" in markdown

