from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path

from app.hwp.application import HwpApplication, HwpAutomationError


@dataclass(frozen=True)
class NumberCharacterStyle:
    face_name_hangul: str
    face_name_latin: str
    face_name_hanja: str
    face_name_symbol: str
    height: int
    bold: int
    italic: int
    underline_type: int
    text_color: int
    shade_color: int


@dataclass(frozen=True)
class NumberingStyleProfile:
    num_format_level0: int
    str_format_level0: str
    alignment_level0: int
    auto_indent_level0: int
    text_offset_type_level0: int
    text_offset_level0: int
    width_adjust_level0: int
    use_inst_width_level0: int
    character: NumberCharacterStyle | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "NumberingStyleProfile":
        character_data = data.get("character")
        character = (
            NumberCharacterStyle(**character_data)  # type: ignore[arg-type]
            if isinstance(character_data, dict)
            else None
        )
        values = dict(data)
        values["character"] = character
        return cls(**values)  # type: ignore[arg-type]


def capture_numbering_style(hwp: HwpApplication) -> NumberingStyleProfile:
    parameter = hwp.automation.HParameterSet.HParaShape
    hwp.automation.HAction.GetDefault("ParagraphShape", parameter.HSet)
    if int(parameter.HeadingType) != 2:
        raise HwpAutomationError("Current paragraph does not use paragraph numbering")
    numbering = parameter.Numbering
    character = None
    if int(numbering.HasCharShapeLevel0):
        shape = numbering.CharShapeLevel0
        character = NumberCharacterStyle(
            face_name_hangul=str(shape.FaceNameHangul),
            face_name_latin=str(shape.FaceNameLatin),
            face_name_hanja=str(shape.FaceNameHanja),
            face_name_symbol=str(shape.FaceNameSymbol),
            height=int(shape.Height),
            bold=int(shape.Bold),
            italic=int(shape.Italic),
            underline_type=int(shape.UnderlineType),
            text_color=int(shape.TextColor),
            shade_color=int(shape.ShadeColor),
        )
    return NumberingStyleProfile(
        num_format_level0=int(numbering.NumFormatLevel0),
        str_format_level0=str(numbering.StrFormatLevel0),
        alignment_level0=int(numbering.AlignmentLevel0),
        auto_indent_level0=int(numbering.AutoIndentLevel0),
        text_offset_type_level0=int(numbering.TextOffsetTypeLevel0),
        text_offset_level0=int(numbering.TextOffsetLevel0),
        width_adjust_level0=int(numbering.WidthAdjustLevel0),
        use_inst_width_level0=int(numbering.UseInstWidthLevel0),
        character=character,
    )


def capture_number_character_style(hwp: HwpApplication) -> NumberCharacterStyle:
    """Read the character shape at the caret without changing the document.

    Many supplied templates show an example number as literal text instead of
    a Hangul paragraph-numbering object.  In that case there is no numbering
    definition to copy, but its font, size and emphasis are still useful when
    the assembler creates real paragraph numbers later.
    """
    shape = hwp.automation.HParameterSet.HCharShape
    hwp.automation.HAction.GetDefault("CharShape", shape.HSet)
    return NumberCharacterStyle(
        face_name_hangul=str(shape.FaceNameHangul),
        face_name_latin=str(shape.FaceNameLatin),
        face_name_hanja=str(shape.FaceNameHanja),
        face_name_symbol=str(shape.FaceNameSymbol),
        height=int(shape.Height),
        bold=int(shape.Bold),
        italic=int(shape.Italic),
        underline_type=int(shape.UnderlineType),
        text_color=int(shape.TextColor),
        shade_color=int(shape.ShadeColor),
    )


def capture_template_numbering_style(
    hwp: HwpApplication,
    fallback: NumberingStyleProfile,
    *,
    literal_format: str | None = None,
) -> tuple[NumberingStyleProfile, str]:
    """Capture the best numbering style available at the current caret.

    Native Hangul numbering wins.  For a literal ``1.``/``1)`` example we
    retain the neutral numbering geometry from ``fallback`` and replace only
    the visible format and character shape.  If even character-shape reading
    is unavailable, the neutral fallback remains usable and the caller gets a
    precise source label for its audit log.
    """
    try:
        return capture_numbering_style(hwp), "template-native-numbering"
    except Exception:
        pass
    try:
        character = capture_number_character_style(hwp)
    except Exception:
        return fallback, "generic-fallback"
    return (
        replace(
            fallback,
            str_format_level0=literal_format or fallback.str_format_level0,
            character=character,
        ),
        "template-literal-number-character-shape",
    )


def apply_numbering_style(
    hwp: HwpApplication,
    profile: NumberingStyleProfile,
    *,
    number: int,
) -> None:
    # Hancom may create a list, create its character style, and assign the
    # requested font on successive ParagraphShape executions. A successful
    # Execute alone does not mean the number's font has actually been set.
    for _ in range(3):
        _set_numbering_style(hwp, profile, number=number)
        if numbering_styles_visually_equivalent(profile, capture_numbering_style(hwp)):
            return
    raise HwpAutomationError(f"{number}번 문단의 번호 서식을 양식과 일치시킬 수 없습니다.")


def _set_numbering_style(
    hwp: HwpApplication, profile: NumberingStyleProfile, *, number: int
) -> None:
    if number < 1:
        raise ValueError("Visible question numbers must be positive")
    parameter = hwp.automation.HParameterSet.HParaShape
    hwp.automation.HAction.GetDefault("ParagraphShape", parameter.HSet)
    parameter.HeadingType = 2
    parameter.Level = 0
    numbering = parameter.Numbering
    numbering.NewList = 1
    numbering.StartNumber = number
    numbering.StartNumber0 = number
    numbering.NumFormatLevel0 = profile.num_format_level0
    numbering.StrFormatLevel0 = profile.str_format_level0
    numbering.AlignmentLevel0 = profile.alignment_level0
    numbering.AutoIndentLevel0 = profile.auto_indent_level0
    numbering.TextOffsetTypeLevel0 = profile.text_offset_type_level0
    numbering.TextOffsetLevel0 = profile.text_offset_level0
    numbering.WidthAdjustLevel0 = profile.width_adjust_level0
    numbering.UseInstWidthLevel0 = profile.use_inst_width_level0
    numbering.HasCharShapeLevel0 = 1 if profile.character is not None else 0
    if profile.character is not None:
        source = profile.character
        target = numbering.CharShapeLevel0
        target.FaceNameHangul = source.face_name_hangul
        target.FaceNameLatin = source.face_name_latin
        target.FaceNameHanja = source.face_name_hanja
        target.FaceNameSymbol = source.face_name_symbol
        target.Height = source.height
        target.Bold = source.bold
        target.Italic = source.italic
        target.UnderlineType = source.underline_type
        target.TextColor = source.text_color
        target.ShadeColor = source.shade_color
    if not hwp.automation.HAction.Execute("ParagraphShape", parameter.HSet):
        raise HwpAutomationError(f"Cannot apply numbering style for number {number}")


def save_numbering_profile(
    profile: NumberingStyleProfile, path: str | Path
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_numbering_profile(path: str | Path) -> NumberingStyleProfile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Numbering profile must be a JSON object")
    return NumberingStyleProfile.from_dict(data)


def numbering_styles_visually_equivalent(
    expected: NumberingStyleProfile, actual: NumberingStyleProfile
) -> bool:
    if (
        expected.num_format_level0 != actual.num_format_level0
        or expected.str_format_level0 != actual.str_format_level0
        or expected.alignment_level0 != actual.alignment_level0
        or expected.auto_indent_level0 != actual.auto_indent_level0
        or expected.text_offset_type_level0 != actual.text_offset_type_level0
        or expected.text_offset_level0 != actual.text_offset_level0
        or expected.width_adjust_level0 != actual.width_adjust_level0
        or expected.use_inst_width_level0 != actual.use_inst_width_level0
    ):
        return False
    if expected.character is None or actual.character is None:
        return expected.character is actual.character
    expected_character = expected.character
    actual_character = actual.character
    common_matches = (
        expected_character.face_name_hangul
        == actual_character.face_name_hangul
        and expected_character.face_name_latin
        == actual_character.face_name_latin
        and expected_character.face_name_symbol
        == actual_character.face_name_symbol
        and expected_character.height == actual_character.height
        and expected_character.bold == actual_character.bold
        and expected_character.italic == actual_character.italic
        and expected_character.underline_type
        == actual_character.underline_type
        and expected_character.text_color == actual_character.text_color
        and expected_character.shade_color == actual_character.shade_color
    )
    if not common_matches:
        return False
    # HWP 2022 can normalize an unused Hanja face to its document default on
    # save. Require an exact Hanja face only when the numbering format itself
    # contains a CJK ideograph that could render with it.
    has_hanja_literal = any(
        "\u3400" <= character <= "\u9fff"
        for character in expected.str_format_level0
    )
    return (
        not has_hanja_literal
        or expected_character.face_name_hanja
        == actual_character.face_name_hanja
    )
