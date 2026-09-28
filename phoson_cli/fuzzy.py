"""Shared fuzzy matching for the CLI pickers.

Single source of truth for the subsequence scorer used by both the
full-screen model picker (``model_picker._filter_models``) and the inline
completion picker (``inline_picker``). Kept dependency-free so either
picker can import it without a cycle.
"""


def fuzzy_score(query: str, text: str) -> int | None:
    """Score how well *query* matches *text* as a subsequence.

    Returns ``None`` when *query* is not a subsequence of *text* (no match),
    otherwise a higher-is-better score that rewards consecutive characters
    and matches at word boundaries. An empty query matches everything with
    score ``0``.
    """
    if not query:
        return 0

    query = query.lower()
    text = text.lower()

    pos = -1
    score = 0
    consecutive_bonus = 0

    for char in query:
        next_pos = text.find(char, pos + 1)
        if next_pos == -1:
            return None

        score += 1
        if next_pos == pos + 1:
            consecutive_bonus += 3
        else:
            consecutive_bonus += max(0, 2 - (next_pos - pos - 1))

        if next_pos == 0 or text[next_pos - 1] in "-_/ .":
            score += 4

        pos = next_pos

    return score + consecutive_bonus - max(0, len(text) - len(query)) // 12
