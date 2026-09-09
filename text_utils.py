"""Text normalization helpers shared across intake, PO import, and the
dashboard's manual edit forms."""
import re

# Common corporate suffixes/abbreviations that should stay fully uppercase
# rather than being title-cased into "Llc", "Inc", etc.
_KEEP_UPPER = {
    "llc", "inc", "co", "corp", "ltd", "lp", "llp", "pc", "dba", "usa", "us",
    "hvac", "dmv", "gps", "cdl", "pg&e", "at&t",
}


def to_proper_case(text: str) -> str:
    """Turns "ACME TRAFFIC SYSTEMS, INC." into "Acme Traffic Systems, Inc."
    Best-effort, not perfect for every vendor name — known corporate
    suffixes are kept uppercase, and any token containing a digit (e.g.
    "3M", "I-80") is left untouched rather than guessed at. A PM/Admin can
    always correct a specific vendor's display name by hand afterward."""
    if not text:
        return text

    def fix_word(word):
        core = word.strip(".,()")
        if not core:
            return word
        if core.lower() in _KEEP_UPPER:
            replacement = core.upper()
        elif any(ch.isdigit() for ch in core):
            replacement = core
        else:
            # Capitalize each letter-run separately so apostrophes/hyphens
            # inside a word ("O'Brien", "Road-Safe") capitalize correctly —
            # str.title() mishandles both of those.
            replacement = re.sub(
                r"[A-Za-z]+",
                lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(),
                core,
            )
        return word.replace(core, replacement, 1)

    return " ".join(fix_word(w) for w in text.split(" "))
