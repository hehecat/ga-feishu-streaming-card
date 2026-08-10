"""text.py 单元测试：normalize / think标签(含跨chunk) / markdown 结构。"""

import pytest

from ga_feishu_streaming_card.text import (
    count_markdown_tables,
    normalize_stream_text,
    split_markdown_blocks,
    strip_think_tags,
)


class TestNormalize:
    def test_crlf_to_lf(self):
        assert normalize_stream_text("a\r\nb") == "a\nb"

    def test_single_blank_line_kept(self):
        assert normalize_stream_text("a\n\nb") == "a\n\nb"

    def test_many_blank_lines_compressed(self):
        assert normalize_stream_text("a\n\n\n\n\nb") == "a\n\nb"

    def test_strip_leading_trailing_whitespace(self):
        assert normalize_stream_text("  hi  \n") == "hi"

    def test_inner_trailing_spaces_kept_on_line(self):
        assert normalize_stream_text("a b  \nc") == "a b  \nc"


class TestStripThink:
    def test_full_think_block_removed(self):
        s = "before\n```think\nsecret plan\n```\nafter"
        assert strip_think_tags(s) == "before\nafter"

    def test_think_block_at_start(self):
        assert strip_think_tags("```think\nx\n```\nrest") == "rest"

    def test_think_block_at_end(self):
        assert strip_think_tags("rest\n```think\nx\n```") == "rest"

    def test_unclosed_think_removes_to_end(self):
        s = "before\n```think\nsecret without close"
        assert strip_think_tags(s) == "before"

    def test_think_with_blank_before_close(self):
        s = "```think\nx\n\n```\nok"
        assert strip_think_tags(s) == "ok"

    def test_chunked_open_tag_removed(self):
        # chunk1 以 ```thi 结尾（残片），chunk2 内完成闭合围栏
        c1 = "head\n```thi"
        c2 = "nk\nsecret\n```\ntail"
        assert strip_think_tags(c1) == "head"
        assert strip_think_tags(c2) == "tail"

    def test_chunk2_pure_fragment_kept(self):
        # 开标签被切成纯残片且 chunk2 无任何围栏痕迹：无可判信息，按可见文本保留
        assert strip_think_tags("nk") == "nk"

    def test_chunked_full_think_across_two_chunks(self):
        c1 = "head\n```think\nse"
        c2 = "cret\n```\ntail"
        assert strip_think_tags(c1) == "head"
        assert strip_think_tags(c2) == "tail"

    def test_chunked_open_then_closed_in_same_chunk_keeps_rest(self):
        c1 = "head\n```think\nx"
        c2 = "\n```\ntail"
        assert strip_think_tags(c2) == "tail"

    def test_partial_tags_removed(self):
        for frag in ["`", "``", "```t", "```th", "```thi", "```thin", "```think"]:
            assert strip_think_tags(frag) == ""

    def test_normal_code_block_kept(self):
        s = "```python\nprint(1)\n```"
        assert strip_think_tags(s) == s

    def test_multiple_think_blocks(self):
        s = "a\n```think\n1\n```\nb\n```think\n2\n```\nc"
        assert strip_think_tags(s) == "a\nb\nc"


class TestMarkdown:
    def test_split_blocks_by_blank_line(self):
        blocks = split_markdown_blocks("one\n\ntwo\n\nthree")
        assert blocks == ["one", "two", "three"]

    def test_split_collapses_extra_blank(self):
        blocks = split_markdown_blocks("a\n\n\n\nb")
        assert blocks == ["a", "b"]

    def test_single_block(self):
        assert split_markdown_blocks("only") == ["only"]

    def test_empty_text(self):
        assert split_markdown_blocks("") == []

    def test_count_tables(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n\n| c |\n|---|\n| 3 |"
        assert count_markdown_tables(md) == 2

    def test_count_tables_no_false_positive(self):
        assert count_markdown_tables("| not a table") == 0
        assert count_markdown_tables("just text") == 0
