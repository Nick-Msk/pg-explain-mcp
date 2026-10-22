"""
Extracts chapter/section headers from plain text documents.

Supports:
  - CHAPTER 1, Chapter One, CHAPTER IV.
  - PART 2, Part Two
  - ГЛАВА 3, Глава III
  - ACT 1, Scene 2, АКТ I
  - Just Roman numerals on a line: "XIV"
  - Arabic numbers with dot/paren: "1. The Beginning"

Filters out false positives by enforcing monotonic numbering
(e.g., ignores "in chapter 3" if we've already seen chapter 10).
"""

import re


class ChapterParser:
    def __init__(self):
        self._patterns = [
            # Pattern 1: Explicit keyword + number + optional title
            re.compile(
                r'^[ \t]*(?:'
                r'CHAPTER|Chapter|PART|Part|BOOK|Book|'
                r'ГЛАВА|Глава|ЧАСТЬ|Часть|КНИГА|Книга|'
                r'ACT|Act|АКТ|Акт|SCENE|Scene|СЦЕНА|Сцена'
                r')[ \t]+([A-Za-z0-9-]+)\b(.*)$',
                re.MULTILINE
            ),
            # Pattern 2: Just a Roman numeral on a line (e.g., "XIV")
            re.compile(r'^[ \t]*([IVXLCDM]+)[ \t]*$', re.MULTILINE),
            # Pattern 3: Arabic number + dot/paren + Capitalized word
            re.compile(r'^[ \t]*(\d+)[.)][ \t]+[A-ZА-Я].*$', re.MULTILINE)
        ]

        self._roman_map = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
        self._roman_re = re.compile(r'^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$', re.IGNORECASE)

        self._units = {
            "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
            "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19
        }
        self._tens = {
            "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90
        }
        self._scales = {"hundred": 100, "thousand": 1000}

    def parse(self, text: str) -> list[tuple[int, str, int]]:
        """
        Parse text and return a list of (position, title, number) for each section header.
        Positions are sorted by text offset.
        """
        if not text:
            return []

        candidates = []
        for pattern in self._patterns:
            for m in pattern.finditer(text):
                pos = m.start()
                num_str = m.group(1)
                num = self._parse_number(num_str)
                if num is None:
                    continue
                title = m.group(0).strip().split("\n")[0][:80]
                candidates.append((pos, title, num))

        candidates.sort(key=lambda x: x[0])

        # Фильтрация монотонности: защита от "как в главе 3" после "Глава 10"
        result = []
        last_num = 0
        for pos, title, num in candidates:
            if num > last_num:
                result.append((pos, title, num))
                last_num = num

        return result

    def _parse_number(self, s: str) -> int | None:
        s = s.strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        if self._roman_re.match(s):
            return self._roman_to_int(s)
        return self._word_to_int(s)

    def _roman_to_int(self, s: str) -> int:
        res, prev = 0, 0
        for c in reversed(s.upper()):
            curr = self._roman_map.get(c, 0)
            if curr < prev:
                res -= curr
            else:
                res += curr
            prev = curr
        return res

    def _word_to_int(self, s: str) -> int | None:
        s = s.lower().replace("-", " ").strip()
        if not s:
            return None
        words = s.split()
        current, result = 0, 0
        for word in words:
            if word in self._units:
                current += self._units[word]
            elif word in self._tens:
                current += self._tens[word]
            elif word in self._scales:
                current *= self._scales[word]
                if word == "thousand":
                    result += current
                    current = 0
            elif word == "and":
                continue
            else:
                return None
        return result + current

