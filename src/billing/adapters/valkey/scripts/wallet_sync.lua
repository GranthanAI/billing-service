-- KEYS: 1 wallet (wal:{account})
-- ARGV: 1 balance (taxable paise, from ledger_balances)
-- Overwrite the mirror, but only when the wallet has no open holds: a hold in flight means the
-- ledger and the mirror legitimately differ until its usage event is committed.
local held = tonumber(redis.call('HGET', KEYS[1], 'held') or '0')
if held > 0 then return 'BUSY' end
redis.call('HSET', KEYS[1], 'balance', ARGV[1])
return 'OK'
