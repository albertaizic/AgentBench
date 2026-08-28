def edits(files):
    src = files["iniforge/loader.py"]
    # Fixes ONLY duplicate handling; leaves loose truthiness intact.
    src = src.replace(
        """            # BUG: first value wins; duplicates are ignored.
            if key not in bucket:""",
        """            if True:""")
    return {"iniforge/loader.py": src}
