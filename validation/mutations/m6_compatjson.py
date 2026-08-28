def edits(files):
    src = files["ledger/codec.py"]
    # Guards future versions but keeps float corruption for v1 numbers.
    src = src.replace('version = payload.get("version", 1)',
                      '''version = int(payload.get("version", 1))
    if version > SUPPORTED_VERSION:
        raise UnsupportedVersion(f"archive version {version} too new")''')
    return {"ledger/codec.py": src}
