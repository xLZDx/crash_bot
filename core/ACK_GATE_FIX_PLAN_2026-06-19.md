# Real-bot ack-gate fix — plan (2026-06-19)

## Problem (root cause, confirmed by code + wire data)
The real bot (`bot_realbet.py`) settled WIN/LOSS from the round's crash multiplier
(`:1999-2005`) and logged `Placed via WS` whenever the WS packet went out + no
explicit reject (`:1876`, `:1901`). It had **no positive landing confirmation** —
a bet sent into a *closed* betting window is silently ignored by the exchange
(no reject frame), yet the bot recorded it as real and advanced the martingale.

Evidence (2026-06-19): bot log claimed ~100 bets/hr; the exchange recorded ~3/hr
(`/api/account` Bill view, balance chains exactly across only ~80 tx in 27 h).
=> ~97% of logged bets were phantom; pnl/WR/counter (15 451 bets) largely fiction;
real balance bled ~$1/day on the few bets that actually landed.

## The wire signal we key on (research, confirmed)
- Identity (`/api/account/get/`): `userId 932402231331845`, `name "ivanK1157"`, USDT.
- Every accepted bet is broadcast as an incoming WS `tb` frame:
  `f1=game_round_id, f2=currency, f3=amount, f5=username`. Decoder verified on a
  REAL captured frame (round 9354376, NGNFIAT, "3869", "ForwarDBet").
- The existing capture filter (`:500`, keyword `crash/bet/cash/round/balance/game`)
  drops our own frame (username has no keyword) — which is also why the bot is
  blind to its own ack today.
- Secondary signal: USDT balance debit via `/api/user/amount/`.

## Design
- **A. ack parser/matcher** — decode `tb`; match `round + USDT + amount + identity`.
- **B. unfiltered hook** — call the matcher in `on_received` BEFORE the keyword gate.
- **C. settle-gate** — after send, wait <= `ACK_TIMEOUT_S (2.0s)` for our ack:
  ack -> settle as today; no ack -> bet NOT placed: no Placed log, no settle, no
  bets/wins/pnl/scale change, audit `bet_unplaced` (ignored by `_scale_from_audit`),
  retry next window.
- **D.** route EVERY bet through `_wait_for_betting_window` (not only post-loss).
- **E.** honest accounting — counters move only on confirmed real bets.
- **F. safety** — N consecutive no-acks (e.g. 6) -> exit + critical alert. Worst
  case (if the exchange does NOT echo our own bet) = bot safely places nothing +
  alerts, never bleeds money. Balance-debit fallback = Commit 3 if needed.

## Build order (gated; commit local then STOP; push needs separate push-GO)
- **Commit 1 (DONE)** — pure ack logic, no live wiring:
  `_parse_tb_frame`, `_amounts_equal`, `_identity_from_account_get`,
  `_tb_is_our_bet`, `_note_tb_ack`, `_bet_ack_fresh`; globals `_last_bet_ack`,
  const `ACK_TIMEOUT_S`. Tests: `tests/test_realbet_ack_match.py` (11),
  `tests/test_realbet_ack_gate.py` (7). Live loop untouched.
- **Commit 2 (DONE)** — live wiring of B/C/E/F: identity loaded from account/get
  in `on_response`; `on_received` records OUR 'tb' echo against the armed
  `_pending_bet`; bet loop arms before send + ACK-GATE waits <= ACK_TIMEOUT_S for
  the echo, else `bet_unplaced` (no settle / no ladder move) + retry; 6 no-acks ->
  exit+flag. Tests: `tests/test_realbet_ack_wiring.py` (7). Live loop now gated.
- **Commit 3 (next, pending first-live-run data)** — D (route EVERY bet through
  `_wait_for_betting_window`, not only post-loss) IF the live confirm-rate is low
  + optional `/api/user/amount/` USDT-debit fallback. Deferred because the ack-gate
  already makes phantom sends harmless (retry, $0 lost); the first live run reveals
  the real confirm rate + confirms the exchange echoes our own 'tb'.

## Scope boundaries
No change to strategy nr512 / cashout / martingale math; paper bots & collector
untouched; no mainnet/testnet anything; `bcgame_session.json` never committed.

## Open empirical unknown (handled defensively)
Whether the exchange echoes OUR own bet in the public `tb` stream (vs a personal
channel). The first live run of Commit 2 confirms it; the no-ack safety means a
wrong assumption costs $0, not money.
