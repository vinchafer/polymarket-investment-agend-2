#!/usr/bin/env bash
# A/B read-only comparison — opens BOTH DBs read-only (mode=ro), never mutates.
# Usage: ab_compare.sh [a1|a2|both] [limit]
A1=/root/polymarket-agent/trading_log.db
A2=/root/polymarket-agent2/trading_log.db
arm="${1:-both}"; LIM="${2:-15}"
Q="SELECT t.id trade_id,t.timestamp bet_time,substr(t.market_question,1,30) market,
   t.action side,t.bet_usdc stake,t.entry_price my_entry,
   json_extract(e.payload,\"\$.wallet_address\") wallet,
   json_extract(e.payload,\"\$.entry_price\") wallet_entry,
   ROUND(t.entry_price-json_extract(e.payload,\"\$.entry_price\"),4) entry_delta,
   json_extract(e.payload,\"\$.model_used\") provider,
   t.resolved,t.won,t.pnl_usdc
   FROM trades t LEFT JOIN agent_events e
     ON e.market_id=t.market_condition_id AND e.event_type=\"RISK_APPROVED\"
   GROUP BY t.id ORDER BY t.id DESC LIMIT $LIM;"
run(){ sqlite3 -header -column -cmd ".timeout 15000" "file:$1?mode=ro" "$Q"; }
case "$arm" in
  a1)   echo "===== ARM A1 (LLM) =====";  run "$A1" ;;
  a2)   echo "===== ARM A2 (copy) ====="; run "$A2" ;;
  both) echo "===== ARM A1 (LLM) =====";  run "$A1"; echo; echo "===== ARM A2 (copy) ====="; run "$A2" ;;
esac
exit 0
