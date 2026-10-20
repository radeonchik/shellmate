"""Web search through a chain of providers: each one that is configured is tried in order,
the next one picks up when it fails, is rate-limited or out of credits.

  tavily      API, 1000 free searches/month, no card (tavily.com) — built for assistants,
              supports "only these domains", returns page text
  brave       API, $5 free credit/month (~1000 searches), card required
  searxng     your own metasearch instance (tools/setup-searxng.sh)
  duckduckgo  HTML endpoint, no key; CAPTCHAs after bursts

Results are cached for a day, so repeated questions don't spend credits or provoke bans."""

import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shellmate import rag

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
MAX_PAGE = 1_500_000  # bytes
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "shellmate" / "search"
CACHE_TTL = 86400
TAVILY_FREE = 1000  # searches per month on the free plan


@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    text: str = ""  # full page text when the provider returns it (Tavily) — saves a fetch


class WebError(Exception):
    pass


class RateLimited(WebError):
    pass


@dataclass
class Settings:
    order: list[str] = field(default_factory=lambda: ["tavily", "brave", "searxng", "duckduckgo"])
    tavily_key: str = ""
    brave_key: str = ""
    searx_url: str = ""


SETTINGS = Settings()
LAST_PROVIDER = ""  # who answered the last search (for --search-setup / debugging)


def configure(order: list[str], tavily_key: str = "", brave_key: str = "", searx_url: str = "") -> None:
    SETTINGS.order = list(order) or Settings().order
    SETTINGS.tavily_key = tavily_key or os.environ.get("TAVILY_API_KEY", "")
    SETTINGS.brave_key = brave_key or os.environ.get("BRAVE_API_KEY", "")
    SETTINGS.searx_url = searx_url


def get(url: str, timeout: float = 8, headers: dict | None = None, data: bytes | None = None) -> tuple[str, str]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8",
                                                         **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        charset = r.headers.get_content_charset() or "utf-8"
        return ctype, r.read(MAX_PAGE).decode(charset, errors="replace")


# --- providers: query, n, domains -> results; raise RateLimited / WebError on failure ---------


def _site_query(query: str, domains: list[str] | None) -> str:
    """Engines without a domain filter get `site:` (one domain — OR-ing isn't reliable everywhere)."""
    return f"site:{domains[0]} {query}" if domains else query


def _usage_path() -> Path:
    return CACHE / f"tavily-{time.strftime('%Y-%m')}.json"


def tavily_usage() -> dict:
    try:
        return json.loads(_usage_path().read_text())
    except (OSError, ValueError):
        return {"used": 0, "exhausted": False}


def _tavily_count(exhausted: bool = False) -> None:
    u = tavily_usage()
    u["used"] += 0 if exhausted else 1
    u["exhausted"] = u["exhausted"] or exhausted
    _usage_path().parent.mkdir(parents=True, exist_ok=True)
    _usage_path().write_text(json.dumps(u))


def _tavily(query: str, n: int, domains: list[str] | None) -> list[Result]:
    if tavily_usage()["exhausted"]:
        raise RateLimited("Tavily: бесплатные запросы на этот месяц закончились")
    body = {"query": query, "max_results": n, "search_depth": "basic", "include_raw_content": "text"}
    if domains:
        body["include_domains"] = domains
    try:
        _, raw = get("https://api.tavily.com/search", 20, data=json.dumps(body).encode(),
                     headers={"Authorization": f"Bearer {SETTINGS.tavily_key}", "Content-Type": "application/json"})
    except urllib.error.HTTPError as e:
        if e.code in (432, 433):  # plan / pay-as-you-go limit
            _tavily_count(exhausted=True)
            raise RateLimited("Tavily: лимит запросов исчерпан") from e
        if e.code == 401:
            raise WebError("Tavily: ключ не подходит (проверь tavily_key в конфиге)") from e
        raise RateLimited(f"Tavily: HTTP {e.code}") from e
    _tavily_count()
    return [Result(r.get("title", ""), r["url"], r.get("content", ""), r.get("raw_content") or "")
            for r in json.loads(raw).get("results", []) if r.get("url")]


def _brave(query: str, n: int, domains: list[str] | None) -> list[Result]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": _site_query(query, domains), "count": n})
    try:
        _, raw = get(url, 12, headers={"X-Subscription-Token": SETTINGS.brave_key, "Accept": "application/json"})
    except urllib.error.HTTPError as e:
        raise (WebError if e.code in (401, 403) else RateLimited)(f"Brave: HTTP {e.code}") from e
    items = json.loads(raw).get("web", {}).get("results", [])
    return [Result(r.get("title", ""), r["url"], rag.html_to_text(r.get("description", "")))
            for r in items if r.get("url")]


def _searxng(query: str, n: int, domains: list[str] | None) -> list[Result]:
    _, raw = get(f"{SETTINGS.searx_url.rstrip('/')}/search?" + urllib.parse.urlencode(
        {"q": _site_query(query, domains), "format": "json"}), 15)
    data = json.loads(raw)
    res = [Result(r.get("title", ""), r["url"], r.get("content", ""))
           for r in data.get("results", [])[:n] if r.get("url")]
    if not res and data.get("unresponsive_engines"):
        # empty because its engines are suspended (CAPTCHA / 429) — that's a failure, not "nothing exists"
        down = ", ".join(e[0] for e in data["unresponsive_engines"])
        raise RateLimited(f"SearXNG: поисковики не ответили ({down})")
    return res


def _duckduckgo(query: str, n: int, domains: list[str] | None) -> list[Result]:
    q = _site_query(query, domains)
    _, body = get("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q}))
    if "anomaly" in body and "result__a" not in body:
        return _duckduckgo_lite(q, n)
    res = []
    for b in re.split(r'<div class="result results_links', body)[1:]:
        a = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not a or "ad_domain" in b:
            continue
        href = html.unescape(a.group(1))
        m = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(m.group(1)) if m else href  # decode exactly once
        if url.startswith("//"):
            url = "https:" + url
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, re.S)
        res.append(Result(rag.html_to_text(a.group(2)), url, rag.html_to_text(snip.group(1)) if snip else ""))
        if len(res) >= n:
            break
    return res


def _duckduckgo_lite(query: str, n: int) -> list[Result]:
    _, body = get("https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query}))
    if "anomaly" in body and "result-link" not in body:
        raise RateLimited("DuckDuckGo: капча после частых запросов")
    res = []
    for m in re.finditer(r"<a[^>]+href=\"([^\"]+)\"[^>]*class='result-link'[^>]*>(.*?)</a>", body, re.S):
        url = html.unescape(m.group(1))
        u = re.search(r"uddg=([^&]+)", url)
        res.append(Result(rag.html_to_text(m.group(2)), urllib.parse.unquote(u.group(1)) if u else url))
        if len(res) >= n:
            break
    return res


PROVIDERS = {"tavily": _tavily, "brave": _brave, "searxng": _searxng, "duckduckgo": _duckduckgo}


def available() -> list[str]:
    ready = {"tavily": bool(SETTINGS.tavily_key), "brave": bool(SETTINGS.brave_key),
             "searxng": bool(SETTINGS.searx_url), "duckduckgo": True}
    return [p for p in SETTINGS.order if ready.get(p)]


def uses_credits() -> bool:
    """True when the first provider is a metered API — then callers should search less."""
    first = available()[:1]
    return first in (["tavily"], ["brave"])


# --- the chain + cache ------------------------------------------------------------------------------


def _cache_file(query: str, n: int, domains: list[str] | None) -> Path:
    key = json.dumps([query.lower().strip(), n, sorted(domains or [])], ensure_ascii=False)
    return CACHE / (hashlib.sha1(key.encode()).hexdigest() + ".json")


def search(query: str, searx_url: str = "", n: int = 6, domains: list[str] | None = None,
           cache: bool = True) -> list[Result]:
    """First provider in the chain that answers wins. `searx_url` is kept for callers that
    pass one explicitly; normally configure() sets it."""
    path = _cache_file(query, n, domains)
    if cache and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return [Result(**r) for r in json.loads(path.read_text())]
    if searx_url and not SETTINGS.searx_url:
        SETTINGS.searx_url = searx_url
    errors = []
    for name in available():
        try:
            res = PROVIDERS[name](query, n, domains)
        except (OSError, ValueError, WebError) as e:  # URLError is an OSError
            errors.append(f"{name}: {e}")
            continue
        if domains:  # engines with `site:` may still leak other domains
            res = [r for r in res if any(urllib.parse.urlsplit(r.url).netloc.endswith(d) for d in domains)]
        if res:
            global LAST_PROVIDER
            LAST_PROVIDER = name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([asdict(r) for r in res], ensure_ascii=False))
            return res
        errors.append(f"{name}: пусто")
    if any(e.endswith(": пусто") for e in errors):  # someone answered: there's just nothing
        return []
    raise RateLimited("все поисковики сейчас недоступны (" + "; ".join(errors) + "). "
                      "Надёжнее всего ключ Tavily: <имя помощника> --search-setup")


def search_many(queries: list[str], searx_url: str = "", n: int = 6) -> list[Result]:
    """Searches several phrasings (the user's own words + the model's rewrite) and interleaves
    the results, so one bad rewrite can't hijack the topic. With a metered API only the
    last phrasing (the model's precise rewrite) is used, to save credits."""
    phrasings = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    if uses_credits():
        phrasings = phrasings[-1:]
    lists, error = [], None
    for q in phrasings:
        try:
            lists.append(search(q, searx_url, n))
        except WebError as e:
            error = e
    if not any(lists) and error:
        raise error
    seen, merged = set(), []
    for rank in range(n):
        for lst in lists:
            if rank < len(lst) and lst[rank].url not in seen:
                seen.add(lst[rank].url)
                merged.append(lst[rank])
    return merged


META_REFRESH = re.compile(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+url=([^"\'>\s]+)', re.I)


def fetch_text(url: str, _hops: int = 2) -> str:
    try:
        ctype, body = get(urllib.parse.quote(url, safe=":/?&=%#+@,;~"))
    except Exception:  # any broken page (bad URL, TLS, http.client errors) just gets skipped
        return ""
    # some static sites (wiki.hypr.land) send no Content-Type at all: sniff the body
    is_html = "html" in ctype or (not ctype and body.lstrip()[:200].lower().startswith(("<!doctype html", "<html")))
    if is_html:
        m = META_REFRESH.search(body[:3000])
        if m and _hops:  # moved pages on static wikis redirect with <meta refresh>
            return fetch_text(urllib.parse.urljoin(url, html.unescape(m.group(1))), _hops - 1)
        return rag.html_to_text(body)
    return body if ctype.startswith("text/") else ""


def gather(query: str, results: list[Result], pages: int = 4) -> list[tuple[str, str]]:
    """Reads the top pages in parallel (unless the provider already gave their text) and
    returns the most relevant chunks (source=url)."""
    top = results[:pages]
    with ThreadPoolExecutor(max_workers=len(top) or 1) as ex:
        texts = list(ex.map(lambda r: r.text or fetch_text(r.url), top))
    items = [(r.url, c) for r, t in zip(top, texts) for c in rag.chunks(t)]
    # snippets are a fallback for pages that refused to load
    items += [(r.url, f"{r.title}. {r.snippet}") for r in results if r.snippet]
    return rag.rank(query, items, k=6)
