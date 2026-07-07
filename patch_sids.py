"""Patch active_options_position.json with correct June 30 security IDs."""
import json
from pathlib import Path

ACTIVE_PATH = Path(r"D:\Trading\active_options_position.json")

CORRECT_SIDS = {
    (24000, "CE"): "71472",
    (24000, "PE"): "71473",
    (24050, "CE"): "79730",
    (24050, "PE"): "79731",
    (24100, "CE"): "79732",
    (24100, "PE"): "79733",
}

d = json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
for c in d["contracts"]:
    key = (c["strike"], c["option_type"])
    if key in CORRECT_SIDS:
        old = c["security_id"]
        c["security_id"] = CORRECT_SIDS[key]
        print(f"  {c['strike']} {c['option_type']}: {old} -> {c['security_id']}")

tmp = ACTIVE_PATH.with_suffix(".tmp")
tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
tmp.replace(ACTIVE_PATH)
print(f"\nPatched {ACTIVE_PATH}")
print("P2 watcher thread will detect the mtime change and resubscribe within 10s.")
