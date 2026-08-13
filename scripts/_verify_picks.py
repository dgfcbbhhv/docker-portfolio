import json
from modules.daily_picks import build_picks
res = build_picks()
print('date:', res.get('date'))
print('=== picks (A股) ===')
for p in res.get('picks', []):
    if p['market'] == 'A':
        print(f"  {p['code']} {p['category']} kelly={p.get('kelly_final')} flags=[{p.get('risk_flags','')}]")
        for l in p.get('lines', []):
            print('    ', l)
print('=== blocked (前5) ===')
for b in res.get('meta', {}).get('blocked', [])[:5]:
    print(' ', b['code'], b.get('flags'))
