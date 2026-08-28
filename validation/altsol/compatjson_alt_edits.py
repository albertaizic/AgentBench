"""Alternative compatjson solution: dispatch-table decoder with a dedicated
amount parser class; structurally different from the reference's inline ifs."""

def edits(files):
    src = files["ledger/codec.py"]
    src = src.replace(
        '''def decode(payload: dict[str, Any]) -> list[Entry]:
    version = payload.get("version", 1)
    # BUG: no guard against versions we do not understand; a v3 archive from
    # the future silently decodes into nonsense instead of failing loudly.
    entries = []
    for raw in payload.get("entries", []):
        amount = raw["amount"]
        if isinstance(amount, float):
            # BUG: v1 wrote amounts as JSON numbers; routing them through
            # binary float corrupts cents (0.1 + 0.2 problems).
            amount = Decimal(str(amount))
        else:
            amount = Decimal(str(amount))
        memo = raw.get("memo") or ""
        entries.append(Entry(raw["account"], amount, memo))
    return entries''',
        '''class _AmountParser:
    """Parses archived amounts without ever routing through binary float
    arithmetic (str/repr round-trips preserve the written decimal text)."""

    @staticmethod
    def parse(raw_amount, *, legacy_number: bool):
        if isinstance(raw_amount, bool):
            raise ValueError("bool is not an amount")
        if isinstance(raw_amount, str):
            return Decimal(raw_amount)
        if isinstance(raw_amount, int):
            return Decimal(raw_amount)
        if isinstance(raw_amount, float):
            text = repr(raw_amount)
            value = Decimal(text)
            if legacy_number and value == value.to_integral_value():
                return value.quantize(Decimal(1))   # v1 wrote 5.0 for 5
            return value
        raise ValueError(f"unsupported amount encoding: {type(raw_amount)!r}")


_VERSION_DECODERS: dict[int, Any] = {}


def decode(payload: dict[str, Any]) -> list[Entry]:
    try:
        version = int(payload.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise UnsupportedVersion("archive version is not an integer") from exc
    if version > SUPPORTED_VERSION:
        raise UnsupportedVersion(
            f"archive version {version} is newer than supported "
            f"{SUPPORTED_VERSION}"
        )
    legacy_numbers = version == 1
    entries: list[Entry] = []
    for raw in payload.get("entries", []):
        amount = _AmountParser.parse(
            raw["amount"], legacy_number=legacy_numbers)
        memo = raw.get("memo") or ""
        entries.append(Entry(raw["account"], amount, memo))
    return entries''')
    return {"ledger/codec.py": src}
