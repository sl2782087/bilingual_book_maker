import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from bs4 import BeautifulSoup
from ebooklib import epub
import pytest

from book_maker.loader.epub_loader import EPUBBookLoader
from book_maker.loader.plan import DisplayResolver, partition_soup
from book_maker.loader.references import ReferenceIndex
from book_maker.reference_context import reference_preamble, reference_scope
from book_maker.translator.base_translator import BatchMismatch


def fixture(body, notes=""):
    book = epub.EpubBook()
    for name, text in [("text/ch.xhtml", body), ("notes.xhtml", notes)]:
        item = epub.EpubHtml(file_name=name)
        item.content = f"<html><body>{text}</body></html>".encode()
        book.add_item(item)
    soup = BeautifulSoup(f"<body>{body}</body>", "html.parser")
    plan = partition_soup(soup, DisplayResolver([]), "text/ch.xhtml")
    index = ReferenceIndex(book)
    for unit in plan.units:
        unit.references = index.for_unit(unit)
    return soup, plan.units


def test_ruby_and_note_are_context_not_source_or_dom_edits():
    body = '<p id="p">彼は<ruby>一<rt>はじめ</rt></ruby>と言う。<a epub:type="noteref" href="../notes.xhtml#n">1</a></p>'
    soup, units = fixture(
        body,
        '<aside id="n" epub:type="footnote">名前の読み。<a role="doc-backlink" href="text/ch.xhtml#p">戻る</a></aside>',
    )
    unit = units[0]
    assert "はじめ" not in unit.text
    assert unit.references["ruby"] == [{"base": "一", "readings": ["はじめ"]}]
    assert unit.references["notes"][0]["source"] == "名前の読み。"
    assert str(soup) == str(BeautifulSoup(f"<body>{body}</body>", "html.parser"))
    with reference_scope([unit]):
        prompt = reference_preamble(unit.text)
    assert "はじめ" in prompt and "名前の読み。" in prompt
    assert reference_preamble(unit.text) == ""


def test_links_are_not_all_notes_and_unsafe_targets_are_reported():
    _, units = fixture(
        '<p>本文<a href="../notes.xhtml#chapter">章</a><a epub:type="noteref" href="https://example.com/#n">外</a><a epub:type="noteref" href="../notes.xhtml#dup">注</a></p>',
        '<h2 id="chapter">章</h2><aside id="dup">一</aside><aside id="dup">二</aside>',
    )
    refs = units[0].references
    assert "notes" not in refs
    assert {row["reason"] for row in refs["unresolved"]} == {
        "nonlocal_or_invalid_reference",
        "missing_or_duplicate_target",
    }


def test_halving_refreshes_context_and_never_changes_source():
    _, units = fixture(
        "<p><ruby>一<rt>はじめ</rt></ruby>。</p><p><ruby>二<rt>ふた</rt></ruby>。</p>"
    )
    prompts = []

    class Model:
        _fatal_error_detected = False

        def translate_list(self, texts):
            raise BatchMismatch("split")

        def translate(self, text):
            prompts.append(reference_preamble(text))
            return "訳"

    loader = EPUBBookLoader.__new__(EPUBBookLoader)
    loader.translate_model = Model()
    loader._note_misalign_recovery = lambda: None
    assert loader._translate_texts_aligned(
        [unit.text for unit in units], units=units
    ) == ["訳", "訳"]
    assert "はじめ" in prompts[0] and "ふた" not in prompts[0]
    assert "ふた" in prompts[1] and "はじめ" not in prompts[1]
    assert reference_preamble(units[0].text) == ""


def test_context_is_thread_local_and_restored_on_failure():
    barrier = Barrier(2)

    def worker(name):
        unit = SimpleNamespace(
            text=name, references={"ruby": [{"base": name, "readings": [name]}]}
        )
        with reference_scope([unit]):
            barrier.wait()
            return reference_preamble(name)

    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(worker, ["alpha", "beta"]))
    assert "beta" not in first and "alpha" not in second
    with pytest.raises(RuntimeError):
        with reference_scope([SimpleNamespace(text="x", references={"notes": []})]):
            raise RuntimeError()
    assert reference_preamble("x") == ""


def test_handoff_has_stable_source_identity_and_output_binding(tmp_path):
    source, target = tmp_path / "source.epub", tmp_path / "out.epub"
    source.write_bytes(b"source")
    target.write_bytes(b"output")
    _, units = fixture("<p><ruby>一<rt>はじめ</rt></ruby>。</p>")
    loader = EPUBBookLoader.__new__(EPUBBookLoader)
    loader.plan_mode = True
    loader.epub_name = str(source)
    loader.translate_model = SimpleNamespace(SUPPORTS_REFERENCE_CONTEXT=True)
    loader._review_jobs = [SimpleNamespace(unit=units[0], global_index=0, job_id="job")]
    loader._review_markers = {}
    loader.p_to_save = ["阿一。"]
    loader._write_review_handoff(target)
    data = json.loads(target.with_suffix(".review.json").read_text())
    assert data["units"][0]["target"] == "阿一。"
    assert data["units"][0]["references"]["ruby"]
    first_id = data["units"][0]["id"]
    loader.p_to_save = ["小一。"]
    loader._write_review_handoff(target)
    assert (
        json.loads(target.with_suffix(".review.json").read_text())["units"][0]["id"]
        == first_id
    )


def test_complete_epub_keeps_body_alignment_and_exports_handoff(tmp_path):
    book = epub.EpubBook()
    book.set_identifier("fixture")
    book.set_title("Fixture")
    book.set_language("ja")
    chapter = epub.EpubHtml(file_name="text/ch.xhtml", title="Chapter")
    chapter.content = '<html><body><p>彼は<ruby>一<rt>はじめ</rt></ruby>と言う。<sup><a epub:type="noteref" href="../notes.xhtml#n">1</a></sup></p></body></html>'.encode()
    note = epub.EpubHtml(file_name="notes.xhtml", title="Notes")
    note.content = '<html><body><aside id="n" epub:type="footnote"><p>名前の読み。</p></aside></body></html>'.encode()
    book.add_item(chapter)
    book.add_item(note)
    book.spine = [chapter, note]
    source = tmp_path / "fixture.epub"
    epub.write_epub(source, book)

    class Model:
        TRANSLATION_ERROR_MARKER = None
        _fatal_error_detected = False
        SUPPORTS_REFERENCE_CONTEXT = True

        def __init__(self, *args, **kwargs):
            self.prompts = []

        def translate(self, text, needprint=True):
            self.prompts.append(reference_preamble(text))
            return "译：" + text

        def translate_list(self, texts):
            return [self.translate(text) for text in texts]

    loader = EPUBBookLoader(
        str(source),
        Model,
        None,
        resume=False,
        language="zh-hans",
        single_translate=True,
    )
    loader.plan_mode = True
    loader.plan_classify = "all"
    loader.make_bilingual_book()
    assert any(
        "はじめ" in prompt and "名前の読み。" in prompt
        for prompt in loader.translate_model.prompts
    )
    target = tmp_path / "fixture_bilingual.epub"
    handoff = json.loads(target.with_suffix(".review.json").read_text())
    assert handoff["references_supported"] is True
    assert all(row["target"] is not None for row in handoff["units"])
    import zipfile

    with zipfile.ZipFile(target) as archive:
        content = next(
            archive.read(name).decode()
            for name in archive.namelist()
            if name.endswith("text/ch.xhtml")
        )
        assert "epub_reference_data" not in content
        assert "はじめ" not in content
        assert 'href="../notes.xhtml#n"' in content


def test_structured_payload_keeps_quoted_reference_source():
    unit = SimpleNamespace(
        text='彼は "一" と言った。',
        references={"ruby": [{"base": "一", "readings": ["はじめ"]}]},
    )
    with reference_scope([unit]):
        assert "はじめ" in reference_preamble(
            json.dumps(
                {"paragraphs": [{"id": 0, "text": unit.text}]}, ensure_ascii=False
            )
        )
