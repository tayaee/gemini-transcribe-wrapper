# On Quota / Rate-Limit (HTTP 429)

The wrapper does **not** retry automatically on 429. When a 429 is detected it:

1. Logs the raw error and the quota category (free-tier daily quota vs short-term rate limit).
2. Prints retry suggestions: "wait about 1 minute for a short-term 429, or wait Xh Ym (sleep Zs) until PT midnight for the daily quota to reset, then re-run."
3. Aborts the rest of the batch (no point burning more calls on a guaranteed 429).
4. Returns exit code `2`.

Sample log on a quota hit:

```
ERROR Rate limit / quota exceeded (429): Error code: 429 - You exceeded your current quota ...
ERROR It looks like you hit the free tier daily quota (25 calls/day).
ERROR You hit the Gemini API rate limits:
  - max 2 API calls per minute
  - max 30 minutes of audio per call
  - max 25 API calls per day (free tier)
ERROR To retry: wait about 1 minute for a short-term 429, or wait 5h 40m (sleep 20400s) until PT midnight for the daily quota to reset, then re-run.
ERROR Switching to a paid tier (enable billing) removes the free-tier limits.
ERROR Aborting batch: quota / rate limit hit while processing <file>. Remaining files will not be processed.
```
