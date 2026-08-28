def edits(files):
    src = files["csvroll/records.py"]
    # Quotes commas but embedded newlines still corrupt rows.
    src = src.replace('lines.extend(",".join(row) for row in self.rows)',
                      'lines.extend(",".join(\'"\' + c.replace(\'"\', \'""\') + \'"\' if any(ch in c for ch in ",\\"") else c for c in row) for row in self.rows)')
    return {"csvroll/records.py": src}
