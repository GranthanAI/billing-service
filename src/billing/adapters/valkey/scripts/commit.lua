-- KEYS: 1 hold  2 wallet  3 holds:due  4 done marker  5 counters for the usage's IST day
--       6 the member month-to-date key named in the hold's mkey (a dummy when it has none)
-- ARGV: 1 due member  2 charged_paise (0 unless paid from credits)  3 model group  4 counter_ttl
if not redis.call('SET', KEYS[4], '1', 'NX', 'EX', 86400) then return 'DUP' end
local held = redis.call('HGET', KEYS[1], 'price')
local mkey = redis.call('HGET', KEYS[1], 'mkey')
if held then
  if tonumber(held) > 0 then redis.call('HINCRBY', KEYS[2], 'held', -tonumber(held)) end
else
  -- the hold expired and its message was given back; the answer came anyway, so count it
  redis.call('HINCRBY', KEYS[5], 'total', 1)
  redis.call('HINCRBY', KEYS[5], ARGV[3], 1)
  redis.call('EXPIRE', KEYS[5], tonumber(ARGV[4]))
end
local charged = tonumber(ARGV[2])
if charged > 0 then
  redis.call('HINCRBY', KEYS[2], 'balance', -charged)
  redis.call('HINCRBY', KEYS[2], 'mtd', charged)
end
-- the member's month-to-date moves from the held price to what was charged; a late commit
-- (no hold) leaves it alone
if held and mkey and mkey ~= '' and mkey == KEYS[6] then
  redis.call('INCRBY', KEYS[6], charged - tonumber(held))
end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return 'OK'
