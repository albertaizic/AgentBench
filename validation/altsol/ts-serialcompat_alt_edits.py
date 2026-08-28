"""Alternative ts-serialcompat solution: decode dispatches through a version
table whose v1 adapter lives in its own module-scope class; encode wraps the
payload in an adapter object serialized via toJSON — structurally different
from the reference's inline fromV1 function."""

def edits(files):
    src = files["src/envelope.js"]
    src = src.replace(
        """export function encode(manifest) {
  return JSON.stringify({ format: FORMAT, version: VERSION, payload: manifest });
}""",
        """class V2Envelope {
  constructor(payload) {
    this.payload = normalizeManifest(payload);
  }
  toJSON() {
    return { format: FORMAT, version: VERSION, payload: this.payload };
  }
}

export function encode(manifest) {
  return JSON.stringify(new V2Envelope(manifest));
}""")
    src = src.replace(
        """export function decode(text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    throw new TypeError("envelope is not valid JSON");
  }
  if (doc === null || typeof doc !== "object") {
    throw new TypeError("not a shipmanifest envelope");
  }
  if (doc.format !== FORMAT && doc.format !== "shipmanifest.v1") {
    throw new TypeError(`not a shipmanifest envelope: format ${String(doc.format)}`);
  }
  if (doc.version !== VERSION) {
    throw new Error(`unsupported envelope version: ${String(doc.version)}`);
  }
  return doc.payload;
}""",
        """const TONS_TO_KG = 1000;

function legacyPayloadToCanonical(legacy) {
  const cargo = Array.isArray(legacy.items)
    ? legacy.items.map((item) => ({
        sku: item.item_code,
        qty: item.quantity,
        weightKg: Number(item.mass_tons) * TONS_TO_KG,
      }))
    : [];
  return normalizeManifest({
    id: legacy.manifest_id,
    vessel: legacy.vessel_name,
    port: legacy.dock,
    sealedAt: legacy.sealed_at,
    cargo,
  });
}

const VERSION_DECODERS = {
  [[FORMAT, VERSION].join("@")]: (doc) => normalizeManifest(doc.payload),
  "shipmanifest.v1": (doc) => legacyPayloadToCanonical(doc.manifest ?? {}),
};

export function decode(text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    throw new TypeError("envelope is not valid JSON");
  }
  if (doc === null || typeof doc !== "object") {
    throw new TypeError("not a shipmanifest envelope");
  }
  const decodeVersion = VERSION_DECODERS[[doc.format, doc.version].join("@")]
    ?? VERSION_DECODERS[doc.format];
  if (decodeVersion === undefined && doc.format !== FORMAT) {
    throw new TypeError(`not a shipmanifest envelope: format ${String(doc.format)}`);
  }
  if (decodeVersion === undefined) {
    throw new Error(`unsupported envelope version: ${String(doc.version)}`);
  }
  return decodeVersion(doc);
}""")
    return {"src/envelope.js": src}
