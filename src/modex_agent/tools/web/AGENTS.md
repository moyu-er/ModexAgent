<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# web

## Purpose
Web interaction tools for agents. Provides URL content fetching (HTML-to-markdown conversion) and web search via DuckDuckGo. Enables agents to read web pages and search the internet programmatically.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `reader.py` | `WebReaderTool(Tool)` — fetch URL content and convert to readable text. Uses `markdownify` for HTML-to-markdown. Static HTML only — JavaScript-rendered pages may not return full content. Configurable `_MAX_CONTENT_LENGTH` (50k chars) and custom `_USER_AGENT`. Fetches ride the guarded transport (`guarded_http`/`guarded_transport`) |
| `guarded_http.py` | Guarded fetch orchestration — `guarded_async_client` (proxy-free client factory, `trust_env=False`), `follow_guarded` (manual redirects, per-hop policy check), `guarded_fetch` (one-shot guarded GET), `GuardedHttpError`/`PolicyBlockedError` |
| `guarded_transport.py` | Guarded transport adaptation — `ValidatingNetworkBackend` (resolve → validate every DNS answer → dial validated IP literal; fail-closed SSRF at connect time), `GuardedAsyncTransport` (httpx↔httpcore adapter), httpcore→httpx error mapping, `AsyncResolver` seam |
| `search.py` | `WebSearchTool(Tool)` — search the web via DuckDuckGo. Returns titles, URLs, and snippets. Requires the `ddgs` (duckduckgo-search) package |

## For AI Agents

### Working In This Directory
- `WebReaderTool` fetches a single URL and returns the content as markdown text
- `WebSearchTool` performs a DuckDuckGo search and returns results with titles, URLs, and snippets
- Both tools require internet connectivity
- `WebReaderTool` has a 50,000 character content limit — pages exceeding this are truncated
- `WebSearchTool` requires the `ddgs` package (duckduckgo-search)

### Common Patterns
- `WebReaderTool.execute({"url": "https://example.com"})` — fetch and read a URL
- `WebSearchTool.execute({"query": "search terms", "max_results": 5})` — search the web
- Both tools return text content that can be fed into LLM context

## Dependencies

### Internal
- `modex_agent.core.tool_manager` — `Tool` ABC, `ToolConfig`

### External
- `httpx` — HTTP client for URL fetching and search API calls
- `markdownify` (optional for WebReaderTool) — HTML-to-markdown conversion
- `ddgs` / `duckduckgo-search` (optional for WebSearchTool) — DuckDuckGo search API

<!-- MANUAL -->
