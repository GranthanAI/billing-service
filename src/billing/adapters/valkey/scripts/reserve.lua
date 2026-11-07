-- KEYS: 1 counters  2 idem  3 hold  4 wallet  5 holds:due
--       6 member month-to-date string mtdm:{wallet}:{uid}:{yyyymm} ('' when there is no cap)
-- ARGV: 1 mode ('plan'|'payg')  2 field (group or 'youtube')  3 limit (-1 = none)
--       4 best_limit (-1 = none)  5 price_paise  6 hold_id  7 now (unix s)
--       8 hold_ttl  9 counter_ttl  10 no_credit_fast_per_day  11 uid
--       12 billing_account_id  13 plan_version_id (stored on the hold for attribution)
--       14 member_cap (-1 = none): an organisation's per-member monthly cap on its wallet,
--          taxable paise. Deliberate deviation from the LLD, which has only the wallet-wide
--          cap. KEYS[6] counts held + committed prices; it is INCRBYed, never reset (the
--          key is per IST month and expires after 40 days).
local prior = redis.call('GET', KEYS[2])
if prior then return prior end                       -- replay a retried ALLOW

local mode, field = ARGV[1], ARGV[2]
local limit, best_limit = tonumber(ARGV[3]), tonumber(ARGV[4])
local price, hold_id, now = tonumber(ARGV[5]), ARGV[6], tonumber(ARGV[7])
local hold_ttl, counter_ttl, grace = tonumber(ARGV[8]), tonumber(ARGV[9]), tonumber(ARGV[10])
local total = tonumber(redis.call('HGET', KEYS[1], 'total') or '0')
local downgraded = 0
local mkey = ''

if mode == 'plan' then
  if limit >= 0 and total >= limit then return 'DENY|LIMIT' end
  if field == 'best' and best_limit >= 0 then
    local best = tonumber(redis.call('HGET', KEYS[1], 'best') or '0')
    if best >= best_limit then field = 'smart'; downgraded = 1 end
  end
  price = 0
else
  local w = redis.call('HMGET', KEYS[4], 'balance', 'held', 'mtd', 'cap')
  local balance, held = tonumber(w[1] or '0'), tonumber(w[2] or '0')
  local mtd, cap = tonumber(w[3] or '0'), tonumber(w[4] or '-1')
  if cap >= 0 and mtd + held + price > cap then return 'DENY|SPEND_CAP' end
  local mcap = tonumber(ARGV[14] or '-1')
  if mcap >= 0 and KEYS[6] ~= '' then
    local member_mtd = tonumber(redis.call('GET', KEYS[6]) or '0')
    if member_mtd + price > mcap then return 'DENY|MEMBER_CAP' end
  end
  if balance - held < price then
    if field ~= 'fast' then return 'DENY|NO_CREDIT' end
    if tonumber(redis.call('HGET', KEYS[1], 'grace') or '0') >= grace then
      return 'DENY|NO_CREDIT'
    end
    redis.call('HINCRBY', KEYS[1], 'grace', 1)       -- free Fast message, no credit
    price = 0
  elseif price > 0 then
    redis.call('HINCRBY', KEYS[4], 'held', price)
    if mcap >= 0 and KEYS[6] ~= '' then
      redis.call('INCRBY', KEYS[6], price)
      redis.call('EXPIRE', KEYS[6], 3456000)       -- 40 days
      mkey = KEYS[6]
    end
  end
end

redis.call('HINCRBY', KEYS[1], 'total', 1)
redis.call('HINCRBY', KEYS[1], field, 1)
redis.call('EXPIRE', KEYS[1], counter_ttl)
redis.call('HSET', KEYS[3], 'counters', KEYS[1], 'field', field, 'price', price, 'mode', mode,
           'acct', ARGV[12], 'pv', ARGV[13], 'mkey', mkey, 'wkey', KEYS[4])
redis.call('EXPIRE', KEYS[3], hold_ttl + 3600)
redis.call('ZADD', KEYS[5], now + hold_ttl, ARGV[11] .. ':' .. hold_id)
-- Deliberate deviation from the LLD text: the new running total is appended as a sixth field so
-- remaining_today needs no second round trip.
local result = 'ALLOW|' .. field .. '|' .. hold_id .. '|' .. price .. '|' .. downgraded .. '|' .. (total + 1)
redis.call('SET', KEYS[2], result, 'EX', 86400)
return result
