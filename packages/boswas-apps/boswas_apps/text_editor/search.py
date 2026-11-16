"""Find and replace over the text of a document (QtCore's regular expressions; no widgets).

Positions are offsets in UTF-16 code units, the positions of a Qt text
cursor: a match can be handed to the editor as it is. The engine is PCRE
through ``QRegularExpression``: it gives up on an expression that would
take forever instead of hanging the program, and ``Query.valid`` is False
then or when the expression is malformed.

A plain query is matched literally; ``whole_word`` wants no letter, digit
or underscore on either side. In a regular expression ``^`` and ``$``
match at the line ends and ``.`` does not match a line end; the replacement
may use ``\\1`` or ``$1`` (``\\0``, ``$0``: the whole match), ``\\n``,
``\\t`` and ``\\\\``.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRegularExpression

MAX_COUNT = 50000              # the count of matches stops here ("50000+")
MAX_QUERY = 10000


@dataclass(frozen=True)
class Options:
    match_case: bool = False
    whole_word: bool = False
    regex: bool = False
    wrap_around: bool = True


class Query:
    """A search text with its options, compiled once."""

    def __init__(self, text: str, options: Options = Options()) -> None:
        self.text = text
        self.options = options
        self.error = ""
        pattern = text if options.regex else QRegularExpression.escape(text)
        if options.whole_word:
            pattern = rf"(?<![\w])(?:{pattern})(?![\w])"
        flags = (QRegularExpression.PatternOption.MultilineOption
                 | QRegularExpression.PatternOption.UseUnicodePropertiesOption)
        if not options.match_case:
            flags |= QRegularExpression.PatternOption.CaseInsensitiveOption
        self.expression = QRegularExpression(pattern, flags)
        if not text:
            self.error = "empty"
        elif len(text) > MAX_QUERY:
            self.error = "The search text is too long."
        elif not self.expression.isValid():
            self.error = f"Not a regular expression: {self.expression.errorString()}."

    @property
    def valid(self) -> bool:
        return not self.error

    def _match(self, text: str, offset: int):
        match = self.expression.match(text, offset)
        while match.hasMatch() and match.capturedLength() == 0:
            # an empty match (x*) is no find: look on from the next position
            offset = match.capturedStart() + 1
            if offset > len(text) * 2:
                return None
            match = self.expression.match(text, offset)
        return match if match.hasMatch() else None

    def find_next(self, text: str, position: int) -> tuple[int, int, bool] | None:
        """The first match at or after ``position``: (start, end, wrapped). None when there is none."""
        if not self.valid:
            return None
        match = self._match(text, max(0, position))
        if match is not None:
            return match.capturedStart(), match.capturedEnd(), False
        if self.options.wrap_around and position > 0:
            match = self._match(text, 0)
            if match is not None and match.capturedStart() < position:
                return match.capturedStart(), match.capturedEnd(), True
        return None

    def find_previous(self, text: str, position: int) -> tuple[int, int, bool] | None:
        """The last match that ends at or before ``position``: (start, end, wrapped)."""
        if not self.valid:
            return None
        found = self._last_before(text, position)
        if found is not None:
            return (*found, False)
        if self.options.wrap_around:
            found = self._last_before(text, None)
            if found is not None and found[0] >= position:
                return (*found, True)
        return None

    def _last_before(self, text: str, limit: int | None) -> tuple[int, int] | None:
        # Look through windows that grow backwards from the limit until one holds a match.
        end = limit if limit is not None else len(text.encode("utf-16-le")) // 2
        window = 4096
        start = max(0, end - window)
        while True:
            last = None
            iterator = self.expression.globalMatch(text, start)
            while iterator.hasNext():
                match = iterator.next()
                if match.capturedEnd() > end or match.capturedStart() >= end:
                    break
                if match.capturedLength() > 0:
                    last = (match.capturedStart(), match.capturedEnd())
            if last is not None or start == 0:
                return last
            window *= 8
            start = max(0, end - window)

    def count(self, text: str, position: int = 0, limit: int = MAX_COUNT) -> tuple[int, int, bool]:
        """(matches in all, how many of them start before ``position``, whether the count stopped at ``limit``)."""
        if not self.valid:
            return 0, 0, False
        total = before = 0
        iterator = self.expression.globalMatch(text)
        while iterator.hasNext():
            match = iterator.next()
            if match.capturedLength() == 0:
                continue
            total += 1
            if match.capturedStart() < position:
                before += 1
            if total >= limit:
                return total, before, True
        return total, before, False

    def replacement(self, match, template: str) -> str:
        """The text a match is replaced with."""
        if not self.options.regex:
            return template
        return expand(template, match)

    def replace_one(self, text: str, start: int, end: int, template: str) -> str | None:
        """The replacement for the selection ``start``..``end`` if it is exactly a match, else None."""
        if not self.valid:
            return None
        match = self.expression.match(text, start)
        if not match.hasMatch() or match.capturedStart() != start or match.capturedEnd() != end:
            return None
        return self.replacement(match, template)

    def replace_all(self, text: str, template: str) -> tuple[str, int]:
        """(``text`` with every match replaced, how many there were)."""
        if not self.valid:
            return text, 0
        units = text.encode("utf-16-le")
        pieces: list[bytes] = []
        position = 0
        count = 0
        iterator = self.expression.globalMatch(text)
        while iterator.hasNext():
            match = iterator.next()
            if match.capturedLength() == 0:
                continue
            pieces.append(units[position * 2:match.capturedStart() * 2])
            pieces.append(self.replacement(match, template).encode("utf-16-le", "surrogatepass"))
            position = match.capturedEnd()
            count += 1
        if not count:
            return text, 0
        pieces.append(units[position * 2:])
        return b"".join(pieces).decode("utf-16-le", "surrogatepass"), count


def expand(template: str, match) -> str:
    """A replacement template with the groups of ``match`` filled in (see the module text)."""
    result: list[str] = []
    index = 0
    length = len(template)
    last_group = match.lastCapturedIndex()
    while index < length:
        character = template[index]
        following = template[index + 1] if index + 1 < length else ""
        if character in "\\$" and following.isdigit() and following.isascii():
            group = int(following)
            result.append(match.captured(group) if group <= last_group else "")
            index += 2
        elif character == "\\" and following in ("n", "t", "\\", "$"):
            result.append({"n": "\n", "t": "\t", "\\": "\\", "$": "$"}[following])
            index += 2
        else:
            result.append(character)
            index += 1
    return "".join(result)


def utf16_length(text: str) -> int:
    """The length of ``text`` in UTF-16 code units (the positions of a Qt document)."""
    return len(text.encode("utf-16-le")) // 2
