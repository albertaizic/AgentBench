"""Alternative prefsfile: version-dispatch table + recursive string-bool
normalization pass applied BEFORE merging defaults."""

def edits(files):
    src = files["prefsfile/settings.py"]
    src = src.replace(
        """def load_settings(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = data.get("schema")
    if version is not None and version != SCHEMA_VERSION:
        raise ValueError(f"unsupported settings schema: {version}")
    settings = {**DEFAULTS}
    for key, value in data.items():
        if key != "schema":
            settings[key] = value
    return settings""",
        """def _normalize_v1_tree(value):
    \"\"\"Recursively coerce JSON trees the way v1 writers serialized them.\"\"\"
    if isinstance(value, dict):
        return {k: _normalize_v1_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_v1_tree(v) for v in value]
    if isinstance(value, str):
        word = value.strip().lower()
        if word == "true":
            return True
        if word == "false":
            return False
    return value


_VERSION_HANDLERS = {
    1: lambda data: {_k: _normalize_v1_tree(_v) for _k, _v in data.items()},
    2: lambda data: data,
}


def load_settings(path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    version = raw.pop("schema", 1)
    handler = _VERSION_HANDLERS.get(version)
    if handler is None:
        raise ValueError(f"unsupported settings schema: {version}")
    settings = {**DEFAULTS}
    settings.update(handler(raw))
    return settings""")
    return {"prefsfile/settings.py": src}
