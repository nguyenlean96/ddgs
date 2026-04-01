# DDGS Concurrency Architecture

## Overview

The DDGS library uses a **threaded metasearch architecture** where multiple search engines are queried concurrently, their results are aggregated, ranked, and returned to the caller. This document describes how the concurrency model works under the hood.

---

## High-Level Flow

```
User Input (Query)
       ↓
DDGS.text(query, ...)
       ↓
_search("text", query, ...)
       ↓
_get_engines() → Retrieve & Initialize Engine Instances
       ↓
ThreadPoolExecutor.submit() × N engines
       ↓
Each Engine Concurrently:
  - build_payload()
  - HTTP GET/POST request
  - extract_results()
  - post_extract_results()
       ↓
Results Aggregator (Deduplication via href/url/image/embed_url)
       ↓
SimpleFilterRanker.rank()
       ↓
Return [:max_results]
```

---

## Core Components

### 1. DDGS Class (Main Entry Point)

**File:** `ddgs/ddgs.py`

```python
class DDGS:
    threads: ClassVar[int | None] = None  # Configurable worker count
    _executor: ClassVar[ThreadPoolExecutor | None] = None  # Global shared pool
```

#### Class Variables
- `threads`: External configuration for controlling concurrency level. If `None`, defaults to `min(32, os.cpu_count() + 4)`.
- `_executor`: Lazy-initialized, cached `ThreadPoolExecutor` instance shared across all `DDGS` instances in the process.

#### Initialization
```python
def __init__(self, proxy: str | None = None, timeout: int | None = 5, *, verify: bool | str = True):
    self._proxy = ...
    self._timeout = timeout
    self._verify = verify
    self._engines_cache = {}  # Per-instance cache of engine objects
```

#### The Executor Pattern
```python
@classmethod
def get_executor(cls) -> ThreadPoolExecutor:
    if cls._executor is None:
        cls._executor = ThreadPoolExecutor(max_workers=cls.threads, thread_name_prefix="DDGS")
    return cls._executor
```

**Key Design Decision:** The executor is a **class variable**, meaning all instances of `DDGS` share a single global thread pool. This reduces overhead but can lead to thread starvation under extreme concurrent load (see [SCALABILITY.md](./SCALABILITY.md)).

---

### 2. Engine Resolution (_get_engines)

**Location:** `ddgs/ddgs.py`, lines 75-125

```python
def _get_engines(self, category: str, backend: str) -> list[BaseSearchEngine[Any]]:
    """Resolve and instantiate the requested search engines."""
```

#### Steps:

1. **Parse Backend String**
   ```python
   backend_list = [x.strip() for x in backend.split(",")]
   ```
   Examples: `"auto"`, `"duckduckgo,google"`, `"all"`

2. **Shuffle Available Keys**
   ```python
   engine_keys = list(ENGINES[category].keys())
   shuffle(engine_keys)  # Randomize order for load balancing
   ```

3. **Prioritize Text Search**
   If category is `"text"` and backend is `"auto"`:
   ```python
   keys = ["wikipedia", "grokipedia"] + [k for k in keys if k not in ("wikipedia", "grokipedia")]
   ```
   This ensures Wikipedia and Grokipedia are evaluated early (they're slower but sometimes provide summaries).

4. **Instantiate & Cache**
   ```python
   for engine_class in engine_classes:
       if engine_class in self._engines_cache:
           instances.append(self._engines_cache[engine_class])
       else:
           engine_instance = engine_class(proxy=self._proxy, timeout=self._timeout, verify=self._verify)
           self._engines_cache[engine_class] = engine_instance
           instances.append(engine_instance)
   ```
   Per-instance caching means the same engine object is reused for multiple searches within a single `DDGS` instance, but a new `DDGS()` object = new engine instances.

5. **Sort by Priority (with Randomization)**
   ```python
   instances.sort(key=lambda e: (e.priority, random()), reverse=True)
   ```
   - Primary sort: `engine.priority` (descending).
   - Secondary sort: `random()` (randomize engines with identical priority for load balancing).

#### Engine Priorities
- Default: `priority = 1`
- Wikipedia: `priority = 2`
- Grokipedia: `priority = 1.9`

---

### 3. The Concurrent Search Loop (_search)

**Location:** `ddgs/ddgs.py`, lines 129-217

#### Initialization

```python
engines = self._get_engines(category, backend)
len_unique_providers = len({engine.provider for engine in engines})
seen_providers: set[str] = set()

results_aggregator = ResultsAggregator({"href", "image", "url", "embed_url"})
max_workers = len_unique_providers  # Dispatch all engines concurrently
executor = self.get_executor()
futures, err = {}, None
engines_iter = iter(engines)
```

**Key Variables:**
- `seen_providers`: Tracks which providers (e.g., "bing", "google") have already returned results, to avoid duplicate providers.
- `max_workers`: Number of concurrent threads. Now equals the number of unique providers.
- `futures`: Dictionary mapping `Future` objects to their corresponding engine instances.
- `engines_iter`: Iterator over engines for lazy submission.

#### Submission Pattern

```python
def submit_next() -> bool:
    for engine in engines_iter:
        if engine.provider in seen_providers:
            continue  # Skip if this provider already returned results
        future = executor.submit(
            engine.search,
            query,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
            page=page,
            **kwargs,
        )
        futures[future] = engine
        return True
    return False

# Initially submit up to max_workers tasks
for _ in range(max_workers):
    submit_next()
```

This ensures:
1. All unique providers are eventually queried (one per provider).
2. Initial submission is immediate and non-blocking.
3. Duplicate providers are skipped (e.g., both Bing and DuckDuckGo use Bing as their provider).

#### The Main Loop: FIRST_COMPLETED

```python
while futures:
    done, not_done = wait(futures, timeout=self._timeout, return_when=FIRST_COMPLETED)
    
    for f in done:
        f_engine = futures.pop(f)
        try:
            if r := f.result():
                results_aggregator.extend(r)
                seen_providers.add(f_engine.provider)
        except Exception as ex:
            err = ex
            logger.info("Error in engine %s: %r", f_engine.name, ex)
    
    if max_results and len(results_aggregator) >= max_results:
        break  # Early exit when we have enough results
        
    if not done:
        break  # No engines finished before timeout
        
    while len(futures) < max_workers:
        if not submit_next():
            break  # No more engines to submit
```

**Flow:**
1. **Wait for the first engine to finish** using `FIRST_COMPLETED`.
2. **Process its results** and add them to the aggregator.
3. **Immediately replenish the pool** by submitting the next waiting engine.
4. **Check early exit conditions:**
   - If we have `max_results` results, break immediately.
   - If no engines finished before timeout, break.
   - Otherwise, loop back and wait for the next engine.

#### Cleanup

```python
for f in futures:
    f.cancel()
```

Any remaining pending futures are cancelled to prevent wasted network requests after we've already gathered enough results.

---

### 4. Results Aggregation

**File:** `ddgs/results.py`

```python
class ResultsAggregator(ABC, Generic[T]):
    def __init__(self, cache_fields: set[str]):
        self.cache_fields = set(cache_fields)  # Keys to use for deduplication
        self._counter: Counter[str] = Counter()
        self._cache: dict[str, T] = {}
    
    def extend(self, items: list[T]):
        # Deduplicate by the specified keys and add results
```

**Default Dedup Keys for Text Search:**
```python
ResultsAggregator({"href", "image", "url", "embed_url"})
```

This ensures that if two engines return the same webpage (same `href`), it's only kept once.

---

### 5. Results Ranking

**File:** `ddgs/similarity.py`

```python
class SimpleFilterRanker:
    """Rank results based on query token appearance."""
    
    def rank(self, results: list[dict], query: str) -> list[dict]:
        """
        1. Pull Wikipedia results to the top
        2. Bucket results by query token overlap (title+body, title only, body only, neither)
        3. Return: [wikipedia, both, title-only, body-only, neither]
        """
```

**Ranking Algorithm:**
1. Extract search tokens (words longer than 3 characters).
2. For each result, check if tokens appear in title, body, or both.
3. Sort by relevance (both > title > body > neither).
4. Push Wikipedia results to the top of each bucket.

---

## Engine Execution Detail

### BaseSearchEngine Class

**File:** `ddgs/base.py`

```python
def search(self, query: str, region: str = "us-en", ...) -> list[T] | None:
    """Execute the search."""
    payload = self.build_payload(query, region, safesearch, timelimit, page, **kwargs)
    
    if self.search_method == "GET":
        html_text = self.request(self.search_method, self.search_url, params=payload)
    else:
        html_text = self.request(self.search_method, self.search_url, data=payload)
    
    if not html_text:
        return None
    
    results = self.extract_results(html_text)
    return self.post_extract_results(results)
```

### Engine Subclass Example: DuckDuckGo

**File:** `ddgs/engines/duckduckgo.py`

```python
class Duckduckgo(BaseSearchEngine[TextResult]):
    name = "duckduckgo"
    provider = "bing"  # Uses Bing as the backend data source
    search_url = "https://html.duckduckgo.com/html/"
    search_method = "POST"
    
    def build_payload(self, query: str, region: str, ...) -> dict:
        return {"q": query, "b": "", "l": region, ...}
    
    def post_extract_results(self, results: list[TextResult]) -> list[TextResult]:
        return [r for r in results if not r.href.startswith("https://duckduckgo.com/y.js?")]
```

---

## HTTP Client Layer

**File:** `ddgs/http_client.py`

```python
class HttpClient:
    def __init__(self, proxy: str | None = None, timeout: int | None = 10, *, verify: bool | str = True):
        self.client = primp.Client(
            proxy=proxy,
            timeout=timeout,
            impersonate="random",
            impersonate_os="random",
            verify=verify if isinstance(verify, bool) else True,
            ca_cert_file=verify if isinstance(verify, str) else None,
        )
    
    def request(self, *args, **kwargs) -> Response:
        resp = self.client.request(*args, **kwargs)
        return Response(status_code=resp.status_code, content=resp.content, text=resp.text)
```

**Key Points:**
- Uses `primp` (a Rust-based HTTP client wrapper) for TLS impersonation.
- Randomizes browser impersonation (`impersonate="random"`) on every request to avoid detection.
- Per-engine instantiation means each engine gets its own isolated HTTP client.
- `if resp.status_code == 200:` check is actually done in `BaseSearchEngine.request`, not `HttpClient.request`.

---

## Thread Safety & Concurrency Guarantees

### Thread-Safe Components
✅ `primp.Client`: Documented as thread-safe  
✅ `ResultsAggregator`: No mutable shared state between threads  
✅ `ThreadPoolExecutor`: Standard library, battle-tested

### Potential Issues
⚠️ `DDGS._executor` as a global class variable under extreme load (see [SCALABILITY.md](./SCALABILITY.md))  
⚠️ Engine caching in `_engines_cache` (currently thread-safe due to GIL, but not explicitly synchronized)

---

## Design Patterns Used

1. **Lazy Initialization:** `_executor` is created on first use, not at class definition.
2. **Resource Caching:** Engine instances and HTTP clients are reused to amortize creation overhead.
3. **Iterator Pattern:** `engines_iter` allows lazy submission without maintaining a manual index.
4. **Future-Based Concurrency:** `ThreadPoolExecutor` + `wait(..., FIRST_COMPLETED)` for reactive task management.
5. **Provider Deduplication:** `seen_providers` set prevents duplicate data sources from being queried.

---

## Summary

The DDGS architecture uses a clean, thread-based concurrency model where:
1. Multiple search engines are instantiated eagerly but submitted to the thread pool lazily.
2. The thread pool processes them concurrently, with results being aggregated live.
3. As soon as enough results are gathered, remaining futures are cancelled to free resources.
4. Results are deduplicated, ranked, and returned to the caller.

For detailed performance implications and scalability considerations, see [SCALABILITY.md](./SCALABILITY.md).
