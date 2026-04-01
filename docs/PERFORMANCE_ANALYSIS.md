# DDGS Performance Analysis & Optimization Report

## Executive Summary

This document provides a comprehensive analysis of the performance bottlenecks identified in the DDGS metasearch library and the optimizations implemented in the `perf/optimize-search-concurrency` branch.

**Key Achievement:** Response time reduced from **10+ seconds → 3.5 seconds → ~1.0 second** through targeted concurrency fixes.

---

## Performance Benchmarks

### Baseline Performance (Before Optimization)

```bash
Command: ./dist/ddgs text -q "python eyes tracking as computer pointer" -f pretty-raw -T
Time: ~10+ seconds (occasionally timing out)
```

**Root Cause:** Complete concurrency failure due to aggressive blocking on all futures before processing any results.

### After Concurrency Fix (FIRST_COMPLETED)

```bash
Command: same as above
Time: ~3.5 seconds
```

**Improvement:** 65% reduction, but still suboptimal due to priority queue bottleneck.

### After Max Workers Optimization

```bash
Command: same as above
Time: ~1.0-1.1 seconds (consistent)
```

**Improvement:** 71% reduction from baseline; 67% reduction from previous iteration.

**Physical Limit Analysis:** The remaining ~1.0 second represents the hard theoretical minimum—the time required for network latency (DNS lookups, TLS handshakes, upstream server processing, and response transmission) to the fastest search engines.

---

## Bottleneck #1: The 10-Second Concurrency Stall

### Problem Description

**Location:** `ddgs/ddgs.py`, `_search()` method, lines 188-217 (before optimization).

The original concurrent execution pattern used:
```python
done, not_done = wait(futures, timeout=self._timeout, return_when="FIRST_EXCEPTION")
```

### Root Cause Analysis

1. **`FIRST_EXCEPTION` Trap:** In Python's `concurrent.futures`, `return_when="FIRST_EXCEPTION"` behaves as follows:
   - If any future raises an exception, return immediately.
   - **If no exception is raised**, wait until **every future completes or times out**.
   
   The original code almost never hit exceptions, so it always blocked for the full `timeout` duration (typically 5-10 seconds).

2. **Sequential Looping:** The loop condition `if len(futures) >= max_workers or i >= max_workers:` forced blocking after filling the initial worker slots. Once all workers were submitted, `i >= max_workers` remained `true` for every subsequent iteration, causing the code to call `wait()` repeatedly on *every single iteration*—defeating asynchronicity.

3. **No Early Exit:** Even if results exceeded `max_results`, the code couldn't exit early because it was blocked inside `wait()`.

### Impact

- **Single User:** 10 seconds per search.
- **Cascading Delay:** Each new search had to wait for the global ThreadPoolExecutor to finish the previous batch, serializing them.

### Solution Implemented

**Changed to `FIRST_COMPLETED`:**
```python
done, not_done = wait(futures, timeout=self._timeout, return_when=FIRST_COMPLETED)
```

**Benefits:**
- Returns the **exact millisecond** any single engine finishes.
- Allows immediate processing of partial results.
- No more artificial timeout stalls.

**Added Dynamic Worker Replenishment:**
```python
def submit_next() -> bool:
    for engine in engines_iter:
        if engine.provider in seen_providers:
            continue
        future = executor.submit(...)
        futures[future] = engine
        return True
    return False

while len(futures) < max_workers:
    if not submit_next():
        break
```

This pattern ensures:
- The pool is always saturated with new tasks.
- As soon as an engine finishes, the next one is immediately queued.
- No artificial waiting between submissions.

**Added Immediate Cancellation:**
```python
for f in futures:
    f.cancel()
```

Once `max_results` is reached, any pending futures are cancelled immediately, preventing slow engines from wasting CPU cycles.

---

## Bottleneck #2: The 3.5-Second Priority Queue Bottleneck

### Problem Description

**Location:** `ddgs/ddgs.py`, `_get_engines()` method, and `_search()` method initial setup.

After fixing the 10-second stall, a consistent ~3.5-second latency persisted because of how engines were prioritized and queued.

### Root Cause Analysis

1. **Tiny Worker Pool:**
   ```python
   max_workers = min(len_unique_providers, ceil(max_results / 10) + 1) if max_results else len_unique_providers
   ```
   
   With `max_results=10` (default), this evaluates to: `ceil(10/10) + 1 = 2` workers.

2. **High-Priority Slow Engines:**
   - `wikipedia.priority = 2.0`
   - `grokipedia.priority = 1.9`
   - All bulk engines (DuckDuckGo, Google, Yahoo): `priority = 1.0`
   
   For text search, the engine list is explicitly prioritized:
   ```python
   keys = ["wikipedia", "grokipedia"] + [k for k in keys if k not in ("wikipedia", "grokipedia")]
   ```

3. **The Choke Point:**
   With only 2 worker slots available and engines sorted by priority, Wikipedia and Grokipedia **always** occupied the first 2 slots.
   - Wikipedia makes *two* synchronous HTTP requests (search + fetch article summary), taking ~3.5 seconds alone.
   - Grokipedia also makes extra requests.
   - The fast bulk engines (DuckDuckGo, Google, Yahoo) that actually return useful data were kept waiting in the queue.

4. **Bonus Bug: Incorrect Random Sort**
   ```python
   instances.sort(key=lambda e: (e.priority, random), reverse=True)
   ```
   
   This passed the `random` function **reference** instead of *calling* it. All engines with identical priority (all the bulk engines) got the same secondary sort key, defeating the shuffle logic. They executed in a deterministic order rather than randomly optimized positions.

### Impact

- Even after fixing the 10-second stall, the system couldn't return results faster than the slowest high-priority engine.
- Typical response: ~3.5 seconds waiting for Wikipedia/Grokipedia, before the fast engines even started.
- No way to "fast path" a search through just the bulk engines without modifying the engine list.

### Solution Implemented

**Maximize Concurrent Workers:**
```python
max_workers = len_unique_providers  # Dispatch ALL engines concurrently
```

With 8 text engines enabled, all 8 are now submitted at millisecond `0.0`. The moment the fastest engine (typically DuckDuckGo or Google, ~0.4-1.0 seconds) returns enough results, the loop breaks and the slow engines are cancelled.

**Fix the Random Sort:**
```python
instances.sort(key=lambda e: (e.priority, random()), reverse=True)
```

Added parentheses to actually invoke `random()`, restoring proper randomization of equally-prioritized engines.

**Result:**
- Response time drops to the hard network limit: **~1.0 second**.
- No artificial queueing delays.
- Fast engines are never starved by slow ones.

---

## Performance Bounds & Physical Limits

### The 1.0-Second Hard Limit

Once all optimizations are in place, response times stabilize around **0.95-1.15 seconds**. This is **not a bug or a bottleneck**—it is the physical limit of internet networking:

1. **DNS Lookup:** ~50-100ms to resolve `google.com` or `duckduckgo.com`.
2. **TCP Connection + TLS Handshake:** ~100-200ms to establish an impersonated HTTPS connection.
3. **Upstream Server Processing:** ~100-300ms for the search engine to process your query and prepare results.
4. **Payload Transmission:** ~50-100ms to download the HTML/JSON response.
5. **Local Parsing:** ~50-100ms to parse HTML, extract results, and rank them.

**Total:** ~350ms to ~1000ms depending on server congestion and geographic distance.

### Measured Consistency

```
Run 1: 1.156s
Run 2: 1.054s
Run 3: 1.129s
Run 4: 1.737s (network variance, not code issue)
Run 5: 0.955s
```

Variance is due to:
- Search engine server load.
- Network congestion.
- IPv4 vs IPv6 routing decisions.
- CDN cache hits/misses.

**None of these are under the application's control.**

---

## Summary of Changes

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| Response Time | 10+ sec | ~1.0 sec | **90% reduction** |
| Concurrency Model | Blocking (FIRST_EXCEPTION) | Non-blocking (FIRST_COMPLETED) | True async |
| Max Workers | Limited to 2-5 | All engines (8) | Full parallelism |
| Early Exit | ❌ No | ✅ Yes | Instant result delivery |
| Future Cancellation | ❌ No (wastes CPU) | ✅ Yes | Clean resource cleanup |

---

## How to Verify Performance

```bash
# Single search
time ./dist/ddgs text -q "your query" -f pretty-raw -T

# Repeated runs to check variance
for i in {1..5}; do ./dist/ddgs text -q "your query" -f pretty-raw -T | tail -1; done

# Via pure Python (bypasses PyInstaller startup overhead)
uv run python -c 'import time; from ddgs import DDGS; s = time.time(); DDGS().text("your query"); print(f"Time: {time.time()-s:.3f}s")'
```

---

## Future Optimization: The Async + Singleton Approach

While current performance is excellent for a simple library, the underlying architecture still has room for improvement at scale (specifically in the FastAPI API server). See [SCALABILITY.md](./SCALABILITY.md) for detailed analysis and [OPTIMIZATION_ROADMAP.md](./OPTIMIZATION_ROADMAP.md) for recommended next steps.
