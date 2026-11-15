-- Per-user, per-minute message limit (every plan). Atomic: the counter, the "already counted" marker and the answer.
-- KEYS[1] minute counter, KEYS[2] marker for this request id. ARGV[1] limit (< 0 = off), ARGV[2] ttl seconds.
-- Returns 1 (allowed: counted, or a retry of an already counted request) or 0 (over the limit: nothing consumed).
local limit = tonumber(ARGV[1])
if limit < 0 then return 1 end
if redis.call('EXISTS', KEYS[2]) == 1 then return 1 end
local n = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
if n > limit then
  redis.call('DECR', KEYS[1])
  return 0
end
redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[2]))
return 1
