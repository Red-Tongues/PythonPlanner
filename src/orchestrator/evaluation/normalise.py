"""Deterministic normalisation (PROJECT_PLAN §7 steps 2 and 7; B7).

* ``extract_json`` — tolerant output extraction: strips code fences, preambles and trailing
  prose, and removes trailing commas, before strict JSON parsing.
* ``normalise_quote`` / ``verify_quote`` — *conservative* quote normalisation (R0 GE6
  consensus): whitespace, typographic quotes and dashes, markdown emphasis markers. Nothing else,
  so a verified quote is still the document's own words. Unverified quotes are kept but never
  count, and never fail the parse.
* ``normalise_value`` — the canonical text of a candidate value, used for agreement in SQL:
  scores as integers, bools as ``true``/``false``, text case- and whitespace-folded.
"""

from __future__ import annotations

import json
import re
from typing import Any

from orchestrator.contracts import FieldKind

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
#: typographic quotes, primes, dashes and the no-break space → ASCII (by code point, for clarity)
_TYPOGRAPHIC = str.maketrans(
    {chr(c): "'" for c in (0x2018, 0x2019, 0x201A, 0x201B, 0x2032)}
    | {chr(c): '"' for c in (0x201C, 0x201D, 0x201E, 0x201F, 0x2033)}
    | {chr(c): "-" for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)}
    | {chr(0x00A0): " "}
)
_EMPHASIS = re.compile(r"(\*\*|__|\*|(?<![\w])_(?=\S)|(?<=\S)_(?![\w]))")


class OutputFormatError(ValueError):
    """The model output contains no parseable JSON object."""


def extract_json(text: str) -> Any:
    """Parse the JSON object in a model response, tolerating common small-model noise."""
    candidates: list[str] = [m.group(1) for m in _FENCE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.append(text)
    for candidate in candidates:
        for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise OutputFormatError("output is not valid JSON; reply with one JSON object only")


def normalise_quote(text: str) -> str:
    text = text.translate(_TYPOGRAPHIC)
    text = _EMPHASIS.sub("", text)
    return " ".join(text.split())


def verify_quote(quote: str, source: str) -> bool:
    """True when the quote is an exact substring of the source after conservative normalisation."""
    needle = normalise_quote(quote)
    return bool(needle) and needle in normalise_quote(source)


def normalise_value(kind: FieldKind, value: Any) -> str | None:
    """Canonical text for agreement; ``None`` for NULL (and for an empty tag set)."""
    if value is None:
        return None
    if kind is FieldKind.SCORE:
        return str(int(value))
    if kind is FieldKind.BOOL:
        return "true" if value else "false"
    if kind is FieldKind.TEXT:
        folded = " ".join(str(value).split()).casefold()
        return folded or None
    raise TypeError("tag sets are normalised per tag")
