-- KEYS: 1 hold  2 counters (read from the hold by the caller)  3 wallet  4 holds:due
--       5 the member month-to-date key named in the hold's mkey (a dummy when it has none)
-- ARGV: 1 due member  2 reason ('refund' | 'expire'), for metrics only
local h = redis.call('HMGET', KEYS[1], 'counters', 'field', 'price', 'mode', 'mkey')
if not h[1] then
  redis.call('ZREM', KEYS[4], ARGV[1])
  return 'GONE'                                     -- already committed or released
end
if h[1] ~= KEYS[2] then return redis.error_reply('counter key mismatch') end
local price = tonumber(h[3])
if h[4] == 'payg' and price > 0 then redis.call('HINCRBY', KEYS[3], 'held', -price) end
if h[5] and h[5] ~= '' and h[5] == KEYS[5] and price > 0 then
  redis.call('INCRBY', KEYS[5], -price)
end
redis.call('HINCRBY', KEYS[2], 'total', -1)
redis.call('HINCRBY', KEYS[2], h[2], -1)
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[4], ARGV[1])
return 'OK'
