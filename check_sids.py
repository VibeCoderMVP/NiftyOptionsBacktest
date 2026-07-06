from datetime import date
from src.dhan_instruments import resolve_option_ids

print("Testing fixed resolve_option_ids...")
results = resolve_option_ids([24000, 24050, 24100], date(2026, 6, 30))
for r in results:
    print(f"  {r['strike']} {r['option_type']}  ->  SID={r['security_id']}  seg={r['exchange_segment']}")
