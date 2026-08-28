def edits(files):
    src = files["ledgerpad/tracker.py"]
    # Rejects bad amounts; currency check is case-SENSITIVE (edge skipped).
    src = src.replace(
        "expense = Expense(merchant=merchant, amount_cents=amount_cents, currency=currency)",
        """if amount_cents <= 0:
            raise ValueError("amount must be positive")
        if currency not in VALID_CURRENCIES:
            raise ValueError(f"unknown currency: {currency}")
        expense = Expense(merchant=merchant, amount_cents=amount_cents, currency=currency)""")
    return {"ledgerpad/tracker.py": src}
