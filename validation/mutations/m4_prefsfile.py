def edits(files):
    src = files["prefsfile/settings.py"]
    # Accepts missing schema as v1 but skips string-boolean coercion.
    src = src.replace('version = data.get("schema")', 'version = data.get("schema", 1)')
    return {"prefsfile/settings.py": src}
