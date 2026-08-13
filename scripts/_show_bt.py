import json
d = json.load(open('/vol1/1000/docker-portfolio/data/reports/signal_regime_backtest.json'))
print('=== bench ===', json.dumps(d['bench'], ensure_ascii=False))
print('=== per_stock count:', len(d['per_stock']))
for c, v in d['per_stock'].items():
    h = v['horizon']
    b = h.get('buy', {})
    s = h.get('sell', {})
    b20 = b.get('20d', {})
    s20 = s.get('20d', {})
    bn = b20.get('n', 0)
    bw = b20.get('win_rate', '-')
    sn = s20.get('n', 0)
    sw = s20.get('win_rate', '-')
    print(f"{c} {v['name']:<8} buys={v['buys']:>4} sells={v['sells']:>4} | buy20d n={bn:>4} win={bw:>6} | sell20d n={sn:>4} win={sw:>6}")