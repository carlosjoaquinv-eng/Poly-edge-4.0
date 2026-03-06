#!/bin/bash
echo "=== $(date -u) ===" >> /root/polyedge/v4/logs/health.log
pm2 jlist | python3 -c "
import json,sys
apps=json.load(sys.stdin)
for a in apps:
    print(f\"  {a['name']}: {a['pm2_env']['status']} | restarts={a['pm2_env']['restart_time']}\")" >> /root/polyedge/v4/logs/health.log 2>&1

cat /root/polyedge/v4/data_v4/mm_state.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
inv=d['inventory']
print(f\"  MM: fills={d['fill_count']} spread=\${d['total_spread_captured']:.2f} daily_pnl=\${inv['daily_pnl']:.2f}\")
pos={k:v for k,v in inv['positions'].items() if v['n_trades']>0}
for v in pos.values():
    print(f\"    {v['token_id'][:12]}... net={v['net_position']:.1f} rpnl=\${v['realized_pnl']:.2f} trades={v['n_trades']}\")" >> /root/polyedge/v4/logs/health.log 2>&1

echo "" >> /root/polyedge/v4/logs/health.log
