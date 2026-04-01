# DDGS Optimization Roadmap

## Overview

This document outlines a phased approach to further optimize the DDGS library and prepare it for deployment at larger scales.

---

## Current State (Post-Optimization)

**Branch:** `perf/optimize-search-concurrency`

### What Changed
- ✅ Fixed `FIRST_EXCEPTION` → `FIRST_COMPLETED` concurrency model
- ✅ Implemented dynamic worker replenishment
- ✅ Removed max_workers bottleneck (2 → 8 = all engines)
- ✅ Fixed random sorting bug
- ✅ Added early exit and future cancellation

### Current Performance
- Single user: **~1.0 second** (network-limited)
- Scalability ceiling: **4-8 concurrent users** before thread pool saturation

### Suitable Deployments
- ✅ CLI usage
- ✅ Python scripts
- ✅ Low-traffic APIs (<10 req/sec)

---

## Phase 1: Short-Term Improvements (1-2 weeks)

### 1.1 Configuration Exposure

**Goal:** Allow users to tune concurrency without modifying source code.

**Implementation:**
```python
# In ddgs/ddgs.py
class DDGS:
    # Before: threads: ClassVar[int | None] = None
    # After: Make configurable via environment variable
    threads: ClassVar[int | None] = int(os.environ.get("DDGS_THREADS", "0")) or None
```

**Usage:**
```bash
export DDGS_THREADS=200
python script.py  # Now uses 200 thread pool workers
```

**Effort:** 30 minutes  
**Benefit:** Users can tune for their specific infrastructure without code changes.

---

### 1.2 Add Concurrency Monitoring

**Goal:** Make it easy for deployment teams to understand thread pool health.

**Implementation:**
```python
# In ddgs/ddgs.py
@classmethod
def executor_stats(cls) -> dict[str, int]:
    """Get ThreadPoolExecutor utilization stats."""
    executor = cls.get_executor()
    return {
        "max_workers": executor._max_workers,
        "thread_count": threading.active_count(),
        # Note: ThreadPoolExecutor doesn't expose queue depth directly
        # Consider using custom wrapper or third-party library
    }
```

**Usage:**
```python
from ddgs import DDGS
stats = DDGS.executor_stats()
print(f"Pool: {stats['thread_count']}/{stats['max_workers']} threads in use")
```

**Effort:** 2-4 hours  
**Benefit:** Visibility into thread pool bottlenecks before they cause problems.

---

### 1.3 Update Documentation & Examples

**Goal:** Clarify scalability limitations and provide deployment guidance.

**Deliverables:**
- [x] PERFORMANCE_ANALYSIS.md (created)
- [x] ARCHITECTURE.md (created)
- [x] SCALABILITY.md (created)
- [ ] OPTIMIZATION_ROADMAP.md (this file)
- [ ] Update README.md with performance metrics
- [ ] Add "Deployment Guide" with scale recommendations
- [ ] Code examples for FastAPI integration with tuning hints

**Effort:** 4-6 hours  
**Benefit:** Users deploy DDGS correctly from day one.

---

### 1.4 Add Benchmarking Suite

**Goal:** Make performance regressions visible in CI/CD.

**Implementation:**
```bash
# In tests/bench_concurrency.py

def bench_single_user():
    """Baseline: single client"""
    t0 = time.time()
    DDGS().text("python malware detection")
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"Single user search took {elapsed}s, expected <2.0s"

def bench_concurrent_4():
    """4 concurrent clients"""
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(lambda: DDGS().text(...)) for _ in range(4)]
        for f in futures:
            t0 = time.time()
            f.result()
            elapsed = time.time() - t0
            assert elapsed < 3.0, f"Concurrent search took {elapsed}s, expected <3.0s"
```

**Effort:** 6-8 hours  
**Benefit:** Catch performance regressions before merging.

---

## Phase 2: Mid-Term Refactor (2-4 weeks)

### 2.1 Decouple ThreadPoolExecutor from DDGS Class

**Goal:** Allow per-instance executors and reduce global state.

**Current Design Issue:**
```python
class DDGS:
    _executor: ClassVar[ThreadPoolExecutor | None] = None  # GLOBAL
```

All DDGS instances share a single executor, causing:
- Loss of fine-grained control
- Difficulty in testing (tests interfere with each other)
- Inability to have high-concurrency and low-concurrency searches in the same process

**Proposed Design:**
```python
class DDGS:
    _default_executor: ClassVar[ThreadPoolExecutor | None] = None
    
    def __init__(self, ..., executor: ThreadPoolExecutor | None = None):
        self._executor = executor or self._get_default_executor()
    
    @classmethod
    def _get_default_executor(cls) -> ThreadPoolExecutor:
        if cls._default_executor is None:
            max_workers = int(os.environ.get("DDGS_THREADS", "0")) or None
            cls._default_executor = ThreadPoolExecutor(max_workers=max_workers)
        return cls._default_executor
```

**Usage for High-Concurrency API:**
```python
# Create a dedicated high-capacity executor for the API
api_executor = ThreadPoolExecutor(max_workers=200)

@app.post("/search/text")
async def search_text(request: TextSearchRequest):
    # Use the API's dedicated executor, not the global one
    results = await asyncio.to_thread(
        lambda: DDGS(executor=api_executor).text(request.query)
    )
    return SearchResponse(results=results)
```

**Effort:** 8-12 hours  
**Benefit:** Enables proper separation of concerns and higher concurrency without global state pollution.

---

### 2.2 Implement Engine-Level Timeouts

**Goal:** Prevent a single slow engine from dragging down the entire search.

**Current Design:**
```python
done, not_done = wait(futures, timeout=self._timeout, return_when=FIRST_COMPLETED)
```

The global `self._timeout` applies to the entire wait operation, not individual engines.

**Proposed Design:**
```python
class BaseSearchEngine:
    timeout: int = 5  # Per-engine timeout
    
    def search(self, query: str, ...) -> list[T] | None:
        try:
            # This request will timeout after 5 seconds
            return self._execute_search_with_timeout(query, ...)
        except TimeoutError:
            logger.warning(f"{self.name} timed out after {self.timeout}s")
            return None

# Or use concurrent.futures.TimeoutError + threading
def search(self, query: str, ...) -> list[T] | None:
    try:
        results = self._blocking_search(query, ...)
        return self._post_process(results)
    except Exception as e:
        logger.warning(f"{self.name} failed: {e}")
        return None
```

**Effort:** 4-6 hours  
**Benefit:** Prevents any single slow engine from blocking the entire result set.

---

### 2.3 Add caching Layer

**Goal:** Reduce redundant queries and network traffic.

**Implementation:**
```python
from functools import lru_cache
import hashlib

class DDGSCache:
    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self.cache = {}  # {query_hash: (results, timestamp)}
    
    def get(self, query: str) -> list[dict] | None:
        key = hashlib.md5(query.encode()).hexdigest()
        if key in self.cache:
            results, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return results
        return None
    
    def set(self, query: str, results: list[dict]):
        key = hashlib.md5(query.encode()).hexdigest()
        self.cache[key] = (results, time.time())

# In DDGS
class DDGS:
    def __init__(self, ..., cache: DDGSCache | None = None):
        self._cache = cache
    
    def text(self, query: str, **kwargs) -> list[dict]:
        if self._cache:
            cached = self._cache.get(query)
            if cached:
                return cached
        
        results = self._search("text", query, **kwargs)
        
        if self._cache:
            self._cache.set(query, results)
        
        return results
```

**Usage:**
```python
cache = DDGSCache(ttl_seconds=3600)
results1 = DDGS(cache=cache).text("machine learning")  # Network call
results2 = DDGS(cache=cache).text("machine learning")  # Cache hit, instant
```

**Effort:** 6-8 hours  
**Benefit:** For API servers with repeated queries, 50-90% reduction in network traffic and latency.

---

## Phase 3: Long-Term Async Refactor (4-8 weeks)

### 3.1 Create DDGSAsync Class

**Goal:** Enable true async I/O without threading overhead.

**Implementation Strategy:**

1. **Create `ddgs/ddgs_async.py`:**
```python
import asyncio
from primp import AsyncClient

class DDGSAsync:
    _client: ClassVar[AsyncClient | None] = None
    
    def __init__(self, proxy: str | None = None, timeout: int | None = 5):
        self._proxy = proxy
        self._timeout = timeout
    
    @classmethod
    async def get_client(cls) -> AsyncClient:
        if cls._client is None:
            cls._client = AsyncClient(
                proxy=cls._proxy,
                impersonate="random",
                impersonate_os="random"
            )
        return cls._client
    
    async def text(self, query: str, **kwargs) -> list[dict]:
        """Async text search"""
        engines = self._get_engines("text", kwargs.get("backend", "auto"))
        
        # Create async tasks for all engines
        tasks = [
            self._search_engine_async(engine, query, **kwargs)
            for engine in engines
        ]
        
        # Wait for results as they come in (equivalent to FIRST_COMPLETED)
        results_aggregator = ResultsAggregator({"href", "image", "url", "embed_url"})
        seen_providers = set()
        
        try:
            for coro in asyncio.as_completed(tasks, timeout=self._timeout):
                result = await coro
                if result and result["provider"] not in seen_providers:
                    results_aggregator.extend(result["data"])
                    seen_providers.add(result["provider"])
                
                if len(results_aggregator) >= kwargs.get("max_results", 10):
                    break  # Early exit
        except asyncio.TimeoutError:
            pass
        
        # Cancel remaining tasks
        for task in tasks:
            task.cancel()
        
        return results_aggregator.extract_dicts()
    
    async def _search_engine_async(self, engine, query: str, **kwargs) -> dict | None:
        """Execute a single engine search asynchronously"""
        # ... engine logic ...
```

2. **Update `BaseSearchEngine` to support async:**
```python
class BaseSearchEngine(ABC, Generic[T]):
    async def search_async(self, query: str, ...) -> list[T] | None:
        # ... async implementation ...
```

3. **Update FastAPI endpoints:**
```python
from ddgs.ddgs_async import DDGSAsync

@app.post("/search/text", response_model=SearchResponse)
async def search_text(request: TextSearchRequest) -> SearchResponse:
    results = await DDGSAsync().text(request.query)
    return SearchResponse(results=results)
```

**Benefits:**
- Single event loop handles 1000s of concurrent requests
- No thread starvation
- True "async all the way down"
- ~40% latency reduction due to async I/O efficiency

**Effort:** 30-40 hours  
**Complexity:** High (requires deep understanding of async/await patterns)

---

### 3.2 Global HTTP Client with Cookie Isolation

**Goal:** Keep-Alive connections + isolated user sessions.

**Implementation:**
```python
class DDGSAsync:
    _shared_client: ClassVar[AsyncClient | None] = None
    
    @classmethod
    async def get_shared_client(cls) -> AsyncClient:
        if cls._shared_client is None:
            cls._shared_client = AsyncClient(
                impersonate="random",
                impersonate_os="random",
                cookie_jar=None,  # Disable persistent cookies
            )
        return cls._shared_client
    
    async def _get_isolated_client(self) -> AsyncClient:
        """Get a client with isolation context (for cookie handling)"""
        # Each search creates a fresh context but reuses TCP connections
        client = await self.get_shared_client()
        # Note: primp's AsyncClient may need custom cookie handling
        # Check primp documentation for context/isolation options
        return client
```

**Benefit:** ~300ms faster per search due to TCP/TLS reuse, without cross-user state leakage.

**Effort:** 8-10 hours  
**Risk:** Requires careful testing to ensure no cookie cross-contamination.

---

## Phase 4: Future Enhancements (Optional)

### 4.1 Smart Engine Selection

**Goal:** Only query engines that are likely to return relevant results.

**Implementation:**
```python
class DDGSAsync:
    async def text(self, query: str, **kwargs) -> list[dict]:
        # Analyze query to predict which engines will perform best
        if len(query.split()) == 1:
            # Single-word queries: Fast engines (DDG, Google)
            engines_to_use = [self._get_engine("duckduckgo"), self._get_engine("google")]
        else:
            # Multi-word queries: All engines
            engines_to_use = self._get_engines("text", "auto")
        
        # ... rest of search logic
```

**Benefit:** Faster queries on average (fewer slow engines queried).  
**Risk:** May miss relevant results on some queries.

---

### 4.2 Intelligent Result Ranking

**Goal:** Improve result relevance using ML or semantic similarity.

**Current:** Simple token-based ranking.  
**Proposed:** Semantic similarity using embeddings.

```python
from sentence_transformers import SentenceTransformer

class SmartRanker:
    def __init__(self):
        self.model = SentenceTransformer("all-MiniLM-L6-v2")
    
    def rank(self, results: list[dict], query: str) -> list[dict]:
        query_embedding = self.model.encode(query)
        
        scores = []
        for result in results:
            text = result["title"] + " " + result["body"]
            text_embedding = self.model.encode(text)
            similarity = cosine_similarity([query_embedding], [text_embedding])[0][0]
            scores.append(similarity)
        
        # Sort by similarity
        ranked = sorted(zip(results, scores), key=lambda x: x[1], reverse=True)
        return [r[0] for r in ranked]
```

**Benefit:** Better result relevance.  
**Cost:** Requires additional dependencies + GPU for speed.

---

### 4.3 Fallback & Retry Logic

**Goal:** Improve reliability under adverse network conditions.

**Implementation:**
```python
class DDGSAsync:
    async def text(self, query: str, max_retries: int = 2, **kwargs) -> list[dict]:
        for attempt in range(max_retries):
            try:
                return await self._text_internal(query, **kwargs)
            except (asyncio.TimeoutError, ConnectionError) as e:
                logger.warning(f"Search attempt {attempt} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    raise
```

**Benefit:** Improved reliability on unstable networks.

---

## Implementation Priorities

### High Priority (Do First)
1. ✅ Documentation (Phases 1.3)
2. ✅ Configuration exposure (Phase 1.1)
3. ✅ Monitoring (Phase 1.2)

### Medium Priority (Do Next)
1. Decouple executor (Phase 2.1)
2. Engine-level timeouts (Phase 2.2)
3. Caching layer (Phase 2.3)

### Low Priority (If Scaling Becomes an Issue)
1. Async refactor (Phase 3)
2. Global HTTP client (Phase 3.2)
3. Smart engine selection (Phase 4.1)

---

## Success Metrics

After each phase, measure:

```
- Single-user latency (should stay <1.2s)
- Concurrent user capacity (goal: handle 100+ by end of Phase 3)
- Memory usage (should not grow unbounded)
- Error rate (should remain <0.1%)
- Network efficiency (bytes transferred per search)
```

---

## Timeline Estimate

| Phase | Effort | Timeline |
|-------|--------|----------|
| Phase 1 (Docs + Config) | 15-20 hours | 1-2 weeks |
| Phase 2 (Threading improvements) | 25-30 hours | 2-4 weeks |
| Phase 3 (Async refactor) | 40-50 hours | 4-8 weeks |
| Phase 4 (Optional enhancements) | 20-40 hours | 2-4 weeks |

---

## Conclusion

The DDGS library is already significantly optimized for single-user and low-concurrency scenarios. Future improvements should focus on:

1. **Short-term:** Visibility and configuration
2. **Mid-term:** Decoupling and fine-grained control
3. **Long-term:** True async I/O for high-traffic deployments

No single optimization is critical for most users. Priorities should be driven by actual deployment needs and measured bottlenecks.
