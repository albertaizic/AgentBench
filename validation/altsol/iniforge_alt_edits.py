"""Alternative iniforge solution: hand-rolled integer detection + word sets,
structurally different from the reference (no regex; explicit int branch)."""

def edits(files):
    src = files["iniforge/loader.py"]
    src = src.replace(
        """            # BUG: first value wins; duplicates are ignored.
            if key not in bucket:
                bucket[key] = self._coerce(key, value)""",
        """            # Overwrite so the LAST occurrence of a duplicate key wins.
            bucket[key] = self._coerce(key, value)""")
    src = src.replace(
        """        # BUG: loose truthiness - any non-false word becomes True,
        # so strings like "maybe" silently parse as boolean True and
        # real integers collapse to booleans too.
        lowered = value.lower()
        if lowered in FALSE_WORDS:
            return False
        return True""",
        """        lowered = value.lower()
        if lowered in TRUE_WORDS:
            return True
        if lowered in FALSE_WORDS:
            return False
        digits = value[1:] if value[:1] in ("+", "-") else value
        if digits and digits.isdigit():
            return int(value)
        if not value:
            raise ValueError(f"empty value for {key!r}")
        return value""")
    return {"iniforge/loader.py": src}
