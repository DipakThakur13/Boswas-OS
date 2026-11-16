"""Finding text in the text of pages. No Qt.

The text of a page comes from the PDF engine as one string in which lines
end with a line break. A query matches across such breaks: every run of
white space in the query stands for any run of white space in the text.
Positions are reported in UTF-16 code units, because that is how the
engine counts characters when it is asked where a piece of text is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_QUERY = 500
CONTEXT = 48
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Hit:
    page: int
    start: int              # UTF-16 code units from the start of the page text
    length: int             # UTF-16 code units
    before: str             # the words in front, on one line
    text: str               # what matched, as it stands in the document
    after: str

    def line(self) -> str:
        return f"{self.before}{self.text}{self.after}"


def compile_query(query: str, match_case: bool = False, whole_words: bool = False) -> re.Pattern | None:
    """The pattern for a query typed by the user (plain text, never a regular expression); None for no query."""
    query = query.strip()[:MAX_QUERY]
    if not query:
        return None
    body = r"\s+".join(re.escape(word) for word in _SPACE.split(query))
    if whole_words:
        head = r"(?<!\w)" if query[0].isalnum() or query[0] == "_" else ""
        tail = r"(?!\w)" if query[-1].isalnum() or query[-1] == "_" else ""
        body = f"{head}{body}{tail}"
    return re.compile(body, 0 if match_case else re.IGNORECASE)


def utf16_length(text: str) -> int:
    """How many UTF-16 code units ``text`` takes."""
    if text.isascii():
        return len(text)
    return len(text) + sum(1 for character in text if ord(character) > 0xFFFF)


def _one_line(text: str) -> str:
    return _SPACE.sub(" ", text)


def find_in_page(pattern: re.Pattern, text: str, page: int, limit: int = 2000) -> list[Hit]:
    """Every match of ``pattern`` in the text of one page, in reading order (at most ``limit``)."""
    hits: list[Hit] = []
    if not text:
        return hits
    ascii_only = text.isascii()
    for match in pattern.finditer(text):
        if match.end() == match.start():
            continue
        start, end = match.span()
        before = _one_line(text[max(0, start - CONTEXT):start]).lstrip()
        after = _one_line(text[end:end + CONTEXT]).rstrip()
        if start > CONTEXT:
            before = "…" + before.split(" ", 1)[-1] if " " in before else "…" + before
        if end + CONTEXT < len(text):
            after = (after.rsplit(" ", 1)[0] if " " in after else after) + "…"
        hits.append(Hit(page, start if ascii_only else utf16_length(text[:start]),
                        end - start if ascii_only else utf16_length(text[start:end]),
                        before, _one_line(match.group(0)), after))
        if len(hits) >= limit:
            break
    return hits


def step(count: int, current: int, forward: bool = True) -> tuple[int, bool]:
    """The hit after (or before) ``current`` among ``count`` hits, and whether that wrapped around."""
    if count <= 0:
        return -1, False
    if current < 0:
        return (0, False) if forward else (count - 1, False)
    following = current + (1 if forward else -1)
    if following >= count:
        return 0, True
    if following < 0:
        return count - 1, True
    return following, False


def first_at_or_after(hits: list[Hit], page: int) -> int:
    """The index of the first hit on ``page`` or later; 0 when all hits are before it; -1 without hits."""
    for index, hit in enumerate(hits):
        if hit.page >= page:
            return index
    return 0 if hits else -1


def word_at(text: str, index: int) -> tuple[int, int]:
    """The word around the character ``index`` of ``text`` as (start, end); the character alone if it is no word."""
    if not 0 <= index < len(text):
        return index, index
    if not (text[index].isalnum() or text[index] == "_"):
        return index, index + 1
    start = index
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_'’"):
        start -= 1
    end = index + 1
    while end < len(text) and (text[end].isalnum() or text[end] in "_'’"):
        end += 1
    return start, end


def line_at(text: str, index: int) -> tuple[int, int]:
    """The line around the character ``index`` of ``text`` as (start, end), without its line break."""
    if not 0 <= index < len(text):
        return index, index
    start = max(text.rfind("\n", 0, index), text.rfind("\r", 0, index)) + 1
    ends = [position for position in (text.find("\n", index), text.find("\r", index)) if position >= 0]
    end = min(ends) if ends else len(text)
    return start, max(end, start)
