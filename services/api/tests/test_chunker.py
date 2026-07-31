from deflect.ingest.chunker import chunk_markdown


def test_heading_path_accumulates_nested_headings():
    source = "# Tutorial\n\nIntro text.\n\n## Dependencies\n\nDep text.\n\n### Sub\n\nSub text.\n"

    chunks = chunk_markdown(source)

    assert [c.heading_path for c in chunks] == [
        "Tutorial",
        "Tutorial > Dependencies",
        "Tutorial > Dependencies > Sub",
    ]


def test_sibling_heading_replaces_rather_than_nests():
    source = "# A\n\ntext\n\n## B\n\ntext\n\n## C\n\ntext\n"

    chunks = chunk_markdown(source)

    assert [c.heading_path for c in chunks] == ["A", "A > B", "A > C"]


def test_oversized_section_splits_with_overlap():
    source = "# Long\n\n" + ("word " * 600)

    chunks = chunk_markdown(source, max_chars=400, overlap_chars=50)

    assert len(chunks) > 1
    assert all(c.heading_path == "Long" for c in chunks)
    assert all(len(c.text) <= 400 for c in chunks)
    # The tail of one chunk reappears at the head of the next so a sentence split
    # across the boundary is still retrievable from at least one chunk.
    assert chunks[0].text[-20:] in chunks[1].text


def test_sections_without_body_are_dropped():
    source = "# A\n\n## B\n\n## C\n\nreal content\n"

    chunks = chunk_markdown(source)

    assert [c.heading_path for c in chunks] == ["A > C"]


def test_positions_are_sequential():
    source = "# A\n\ntext\n\n## B\n\ntext\n"

    chunks = chunk_markdown(source)

    assert [c.position for c in chunks] == [0, 1]


def test_fenced_code_containing_hashes_is_not_read_as_a_heading():
    source = "# A\n\n```python\n# not a heading\nx = 1\n```\n"

    chunks = chunk_markdown(source)

    assert [c.heading_path for c in chunks] == ["A"]
    assert "x = 1" in chunks[0].text
