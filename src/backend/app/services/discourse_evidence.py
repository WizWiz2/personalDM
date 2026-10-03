"""Syntactic quotation scope; never a semantic coreference or truth classifier."""
import re
import unicodedata


def quoted_positions(source: str) -> set[int]:
    positions: set[int] = set()
    depth = 0
    ascii_open = False
    for index, char in enumerate(source):
        category = unicodedata.category(char)
        if char == '"' and (index == 0 or source[index - 1] != '\\'):
            ascii_open = not ascii_open
            positions.add(index)
        elif category == 'Pi':
            depth += 1
        elif category == 'Pf':
            positions.add(index)
            depth = max(0, depth - 1)
        if depth or ascii_open:
            positions.add(index)
    return positions


def exclusively_quoted(source: str, designation: str) -> bool:
    """A negative constraint: a literal designation occurs, but only inside quotations.

    Absence, inflected variants and indirect discourse make no positive or negative claim.
    A nonquoted occurrence is NOT proof of participation; semantic validation still applies.
    """
    source = ' '.join(source.casefold().split())
    designation = ' '.join(designation.casefold().split())
    if not designation:
        return False
    starts = [m.start() for m in re.finditer(r'(?<!\w)' + re.escape(designation) + r'(?!\w)', source)]
    positions = quoted_positions(source)
    return bool(starts) and all(start in positions for start in starts)
