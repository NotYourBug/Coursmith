"""Course structure checks bind findings to frozen IDs without private text."""

from dataclasses import replace
import importlib

import pytest

from course_platform.production.models import BriefInput, ChapterView, OutlineItem, OutlineView, ProductionInput


COMPLETE = '''# 沟通课程
## 学习目标
理解倾听的作用。
## 讲解
倾听包括复述和开放问题。
## 案例
讨论团队沟通情境。
## 练习
与同伴角色扮演并记录观察。
## 总结
回顾三个要点。
'''


def checks():
    return importlib.import_module("course_platform.production.checks")


def frozen(*, markdown=COMPLETE, mode="quick", confirmed=False):
    return ProductionInput(
        project_id=1, project_revision=3, mode=mode, brief_revision=1,
        brief=BriefInput(title="沟通", chapter_count=1), references=(),
        outline=OutlineView(10, 1, 1, (OutlineItem(chapter_id="chapter-a", title="倾听", goal="理解"),), confirmed),
        chapters=(ChapterView("chapter-a", 1, markdown),), input_hash="a" * 64,
    )


def codes(result):
    return {finding.code for finding in result.findings}


@pytest.mark.parametrize("mode,confirmed", [("quick", False), ("precise", True)])
def test_nontechnical_exercise_satisfies_course_structure(mode, confirmed):
    result = checks().validate_draft(frozen(mode=mode, confirmed=confirmed))
    assert result.blocking is False
    assert result.human_review_required is True
    assert result.input_hash == "a" * 64
    assert result.findings == ()
    assert not hasattr(result, "approval")


def test_approved_heading_aliases_and_setext_are_sections():
    source = '目标\n====\n理解\n\n讲解\n----\n说明\n\n## 示例\n场景\n## 练习\n讨论\n## 小结\n回顾'
    assert checks().validate_chapter(source, chapter_id="chapter-a") == ()


@pytest.mark.parametrize("source", [
    "学习目标 讲解 案例 练习 总结",
    "```markdown\n## 学习目标\n## 讲解\n## 案例\n## 练习\n## 总结\n```",
    "> ## 学习目标\n> ## 讲解\n> ## 案例\n> ## 练习\n> ## 总结",
    "## `学习目标`\n## `讲解`\n## `案例`\n## `练习`\n## `总结`",
    "<h2>学习目标</h2><h2>讲解</h2><h2>案例</h2><h2>练习</h2><h2>总结</h2>",
], ids=["prose", "fenced", "quoted", "inline-code", "raw-html"])
def test_section_words_outside_real_headings_do_not_satisfy_structure(source):
    findings = checks().validate_chapter(source, chapter_id="chapter-a")
    assert {f.code for f in findings} >= {
        "missing_objectives", "missing_explanation", "missing_example", "missing_exercise", "missing_summary"
    }
    assert all(f.blocking and f.chapter_id == "chapter-a" for f in findings)


@pytest.mark.parametrize("heading,code", [
    ("学习目标", "missing_objectives"), ("讲解", "missing_explanation"),
    ("案例", "missing_example"), ("练习", "missing_exercise"), ("总结", "missing_summary"),
])
def test_each_required_section_is_blocking_when_missing(heading, code):
    result = checks().validate_draft(frozen(markdown=COMPLETE.replace("## " + heading, "普通段落")))
    assert result.blocking is True
    assert code in codes(result)
    assert result.human_review_required is True


@pytest.mark.parametrize("source", ["", " \n\t", "## 学习目标\n## 讲解\n## 案例\n## 练习\n## 总结"])
def test_blank_body_or_empty_sections_block(source):
    result = checks().validate_draft(frozen(markdown=source))
    assert result.blocking
    assert result.human_review_required


def test_chapter_utf8_limit_accepts_exact_bound_and_rejects_one_byte_more():
    boundary = COMPLETE + '😀' * ((204800 - len(COMPLETE.encode('utf-8'))) // 4)
    boundary += 'x' * (204800 - len(boundary.encode('utf-8')))
    assert checks().validate_chapter(boundary, chapter_id="chapter-a") == ()
    findings = checks().validate_chapter(boundary + 'x', chapter_id="chapter-a")
    assert [(f.code, f.chapter_id, f.blocking) for f in findings] == [
        ("chapter_too_large", "chapter-a", True)
    ]


def test_missing_outline_and_missing_body_are_blocking():
    source = frozen()
    assert "missing_outline" in codes(checks().validate_draft(replace(source, outline=None, chapters=())))
    assert "missing_body" in codes(checks().validate_draft(replace(source, chapters=())))
    empty = replace(source.chapters[0], body_revision=None, markdown="")
    assert "missing_body" in codes(checks().validate_draft(replace(source, chapters=(empty,))))


def test_outline_and_chapter_identity_duplicates_and_extras_are_blocking():
    source = frozen()
    outline = replace(source.outline, items=source.outline.items * 2)
    assert "duplicate_outline_chapter" in codes(checks().validate_draft(replace(source, outline=outline)))
    assert "duplicate_chapter" in codes(checks().validate_draft(replace(source, chapters=source.chapters * 2)))
    extra = ChapterView("chapter-other", 1, COMPLETE)
    assert "unexpected_chapter" in codes(checks().validate_draft(replace(source, chapters=(extra,))))
    unidentified = source.outline.items[0].model_copy(update={"chapter_id": None})
    assert "invalid_outline_chapter" in codes(checks().validate_draft(
        replace(source, outline=replace(source.outline, items=(unidentified,)))))
    foreign_outline = replace(source.outline, project_id=2)
    assert "outline_project_mismatch" in codes(checks().validate_draft(replace(source, outline=foreign_outline)))


def test_precise_mode_requires_confirmation_but_never_grants_human_approval():
    result = checks().validate_draft(frozen(mode="precise", confirmed=False))
    assert result.blocking and "outline_unconfirmed" in codes(result)
    assert result.human_review_required


def test_safety_findings_bind_chapter_id_and_never_contain_private_text():
    sentinel = "PRIVATE_SOURCE_SENTINEL"
    source = frozen(markdown=COMPLETE + f'\n[资料](https://alice:{sentinel}@example.test)\n![图](https://example.test/a)')
    result = checks().validate_draft(source)
    assert result.blocking
    assert {"unsafe_link", "image_placeholder"} <= codes(result)
    assert all(f.chapter_id == "chapter-a" for f in result.findings)
    assert sentinel not in repr(result)
    assert "alice" not in repr(result)


def test_finding_and_check_are_immutable_and_human_review_cannot_be_disabled():
    module = checks()
    result = module.DraftCheck("a" * 64, (), False, human_review_required=False)
    assert result.human_review_required is True
    with pytest.raises(AttributeError):
        result.human_review_required = False


def test_subheading_titles_do_not_supply_an_empty_sections_body():
    source = COMPLETE.replace('理解倾听的作用。', '### 空标题')
    result = checks().validate_draft(frozen(markdown=source))
    assert result.blocking
    assert "missing_objectives" in codes(result)


def test_outline_cannot_exceed_sixty_chapters_even_with_unique_ids():
    source = frozen()
    items = tuple(OutlineItem(chapter_id=f"chapter-{index}", title="章节", goal="") for index in range(61))
    result = checks().validate_draft(replace(source, outline=replace(source.outline, items=items)))
    assert result.blocking
    assert "outline_too_large" in codes(result)
