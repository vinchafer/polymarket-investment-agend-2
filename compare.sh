#!/usr/bin/env bash
# compare.sh — A/B read-only comparison (LLM arm A1 vs copy arm A2).
# Everything is filtered from TEST_EPOCH so the pre-reset history never leaks in.
# Opens BOTH DBs read-only via URI; never mutates.
#
# Usage: compare.sh [both|divergence|throughput|verdicts|trades]   (default: both)
set -euo pipefail

A1=/root/polymarket-agent/trading_log.db
A2=/root/polymarket-agent2/trading_log.db
EPOCH=$(grep -h '^TEST_EPOCH=' /etc/polymarket/agent1.env | tail -1 | cut -d= -f2-)
[ -z "${EPOCH:-}" ] && EPOCH="1970-01-01T00:00:00Z"
mode="${1:-both}"

sql() {  # runs a query against an attached-both in-memory session
  sqlite3 -batch ":memory:" -cmd ".timeout 15000" \
    -cmd "ATTACH 'file:${A1}?mode=ro' AS a1" \
    -cmd "ATTACH 'file:${A2}?mode=ro' AS a2" "$@"
}

hdr(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
echo "TEST_EPOCH = $EPOCH   (all metrics counted from here)"

# ---------------------------------------------------------------------------
# 1) Portfolio / trade summary since epoch (both arms)
# ---------------------------------------------------------------------------
summary() {
  hdr "PORTFOLIO SINCE TEST_EPOCH"
  sql -header -column "
    WITH s AS (
      SELECT 'A1-LLM'  arm, resolved, resolution_outcome, won, pnl_usdc, bet_usdc
        FROM a1.trades WHERE timestamp >= '$EPOCH'
      UNION ALL
      SELECT 'A2-COPY' arm, resolved, resolution_outcome, won, pnl_usdc, bet_usdc
        FROM a2.trades WHERE timestamp >= '$EPOCH'
    )
    SELECT arm,
           COUNT(*)                                          trades,
           SUM(resolved=0)                                   open,
           SUM(resolved=1 AND resolution_outcome<>'STALE')   resolved,
           SUM(won=1)                                        wins,
           SUM(won=0 AND resolved=1)                         losses,
           ROUND(COALESCE(SUM(CASE WHEN resolved=1 THEN pnl_usdc END),0),2) pnl_usdc,
           ROUND(COALESCE(SUM(CASE WHEN resolved=0 THEN bet_usdc END),0),2) capital_at_risk
    FROM s GROUP BY arm;"
}

# ---------------------------------------------------------------------------
# 2) Horizon-filter throughput (per arm, per day): total qualifying fills,
#    horizon-rejected, passed. Tells us whether 14d is too tight.
# ---------------------------------------------------------------------------
throughput() {
  hdr "HORIZON THROUGHPUT PER DAY (a=qualifying, b=horizon-rejected, c=passed)"
  sql -header -column "
    WITH ev AS (
      SELECT 'A1-LLM' arm, date(timestamp) d, event_type,
             json_extract(payload,'\$.reason') reason
        FROM a1.agent_events
       WHERE timestamp >= '$EPOCH' AND event_type IN ('NEW_POSITION','HORIZON_REJECTED')
      UNION ALL
      SELECT 'A2-COPY' arm, date(timestamp) d, event_type,
             json_extract(payload,'\$.reason') reason
        FROM a2.agent_events
       WHERE timestamp >= '$EPOCH' AND event_type IN ('NEW_POSITION','HORIZON_REJECTED')
    )
    SELECT arm, d AS day,
           SUM(1)                                    a_qualifying,
           SUM(event_type='HORIZON_REJECTED')        b_horizon_rej,
           SUM(event_type='NEW_POSITION')            c_passed,
           SUM(reason='horizon_exceeded')            r_horizon,
           SUM(reason='no_end_date')                 r_no_end,
           SUM(reason='market_ended')                r_ended
    FROM ev GROUP BY arm, d ORDER BY d, arm;"
}

# ---------------------------------------------------------------------------
# 3) LLM verdict distribution over time (A1 only). If veto-rate stays ~0 the
#    LLM layer is not doing anything — that is itself a test result.
# ---------------------------------------------------------------------------
verdicts() {
  hdr "A1 LLM VERDICT DISTRIBUTION PER DAY (analyst SKIP + devil veto)"
  sql -header -column "
    SELECT date(timestamp) day,
           SUM(event_type='ANALYSIS_COMPLETE')                                          analysed,
           SUM(event_type='ANALYSIS_COMPLETE' AND json_extract(payload,'\$.action')='SKIP') analyst_skip,
           SUM(event_type='CHALLENGE_COMPLETE')                                         challenged,
           SUM(event_type='CHALLENGE_COMPLETE' AND json_extract(payload,'\$.final_verdict')<>'PROCEED') devil_veto,
           SUM(event_type='RISK_REJECTED' AND json_extract(payload,'\$.rejection_reason') LIKE 'Capital at risk%') cap_rej
      FROM a1.agent_events
     WHERE timestamp >= '$EPOCH'
       AND event_type IN ('ANALYSIS_COMPLETE','CHALLENGE_COMPLETE','RISK_REJECTED')
     GROUP BY day ORDER BY day;"
}

# ---------------------------------------------------------------------------
# 4) LLM-DIVERGENCE SET (the metric that decides the test):
#    fills where A1's LLM vetoed (SKIP / devil override, no A1 trade) BUT A2 took
#    the trade. Shows A2's outcome → did A1's veto save money or cost profit?
# ---------------------------------------------------------------------------
divergence() {
  hdr "LLM-DIVERGENCE SET  (A1=llm_rejected & A2=taken)"
  sql -header -column "
    WITH
    a1_taken AS (SELECT DISTINCT market_condition_id cid FROM a1.trades WHERE timestamp>='$EPOCH'),
    a1_llm_veto AS (
      SELECT DISTINCT market_id cid FROM a1.agent_events
       WHERE timestamp>='$EPOCH'
         AND ( (event_type='ANALYSIS_COMPLETE'  AND json_extract(payload,'\$.action')='SKIP')
            OR (event_type='CHALLENGE_COMPLETE' AND json_extract(payload,'\$.final_verdict')<>'PROCEED') )
    ),
    a2_taken AS (
      SELECT market_condition_id cid, market_question q, action, bet_usdc, entry_price,
             resolved, resolution_outcome, won, pnl_usdc
        FROM a2.trades WHERE timestamp>='$EPOCH'
    )
    SELECT substr(t.q,1,40) market, t.action side, t.bet_usdc stake, t.entry_price a2_entry,
           t.resolved, t.resolution_outcome outcome, t.pnl_usdc a2_pnl
      FROM a2_taken t
     WHERE t.cid IN (SELECT cid FROM a1_llm_veto)
       AND t.cid NOT IN (SELECT cid FROM a1_taken)
     ORDER BY t.resolved DESC, t.pnl_usdc;"
  hdr "DIVERGENCE SET — cumulative A2 P&L on A1-vetoed fills"
  sql -header -column "
    WITH
    a1_taken AS (SELECT DISTINCT market_condition_id cid FROM a1.trades WHERE timestamp>='$EPOCH'),
    a1_llm_veto AS (
      SELECT DISTINCT market_id cid FROM a1.agent_events
       WHERE timestamp>='$EPOCH'
         AND ( (event_type='ANALYSIS_COMPLETE'  AND json_extract(payload,'\$.action')='SKIP')
            OR (event_type='CHALLENGE_COMPLETE' AND json_extract(payload,'\$.final_verdict')<>'PROCEED') )
    )
    SELECT COUNT(*) divergent_fills,
           SUM(resolved=1) resolved,
           ROUND(COALESCE(SUM(CASE WHEN resolved=1 THEN pnl_usdc END),0),2) a2_realized_pnl,
           'positive = A1 veto COST profit; negative = A1 veto SAVED money' note
      FROM a2.trades
     WHERE timestamp>='$EPOCH'
       AND market_condition_id IN (SELECT cid FROM a1_llm_veto)
       AND market_condition_id NOT IN (SELECT cid FROM a1_taken);"
}

# ---------------------------------------------------------------------------
# 5) Concordant pairs (both arms took same market) — sizing/entry delta
# ---------------------------------------------------------------------------
concordant() {
  hdr "CONCORDANT PAIRS (both took) — entry/size delta (A1 variable conf vs A2 fix medium)"
  sql -header -column "
    SELECT substr(a.market_question,1,36) market, a.action side,
           a.bet_usdc a1_stake, b.bet_usdc a2_stake,
           ROUND(a.bet_usdc-b.bet_usdc,2) stake_delta,
           a.entry_price a1_entry, b.entry_price b2_entry,
           ROUND(a.entry_price-b.entry_price,4) entry_delta
      FROM a1.trades a JOIN a2.trades b
        ON a.market_condition_id=b.market_condition_id
     WHERE a.timestamp>='$EPOCH' AND b.timestamp>='$EPOCH'
     ORDER BY a.timestamp DESC LIMIT 40;"
}

case "$mode" in
  both)       summary; throughput; verdicts; divergence; concordant ;;
  trades)     summary ;;
  throughput) throughput ;;
  verdicts)   verdicts ;;
  divergence) divergence ;;
  concordant) concordant ;;
  *) echo "unknown mode: $mode (use both|trades|throughput|verdicts|divergence|concordant)"; exit 1 ;;
esac
