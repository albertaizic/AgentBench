"""Alternative ledgerpad: validate via a lookup-driven policy table and
normalize currency at the boundary, structurally unlike the reference."""

def edits(files):
    src = files["ledgerpad/tracker.py"]
    src = src.replace(
        """    def add_expense(self, merchant: str, amount_cents: int, currency: str = "USD") -> Expense:
        expense = Expense(merchant=merchant, amount_cents=amount_cents, currency=currency)
        self.expenses.append(expense)
        return expense""",
        """    def add_expense(self, merchant: str, amount_cents: int,
                    currency: str = "USD") -> Expense:
        # Policy table keeps each rejection reason explicit and testable.
        problems = []
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) \\
                or amount_cents <= 0:
            problems.append("amount must be a positive integer")
        code = (currency or "").strip().upper()
        if code not in VALID_CURRENCIES:
            problems.append(f"unknown currency: {currency}")
        if problems:
            raise ValueError("; ".join(problems))
        expense = Expense(merchant=merchant, amount_cents=amount_cents,
                          currency=code)
        self.expenses.append(expense)
        return expense""")
    return {"ledgerpad/tracker.py": src}
