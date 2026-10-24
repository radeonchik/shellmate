"""Favored sources: official wikis. For how-to and error questions they are searched FIRST;
the open web is only used when they have no answer. Users can add domains in the config.

Search engines rate-limit (and SearXNG's upstream engines get suspended), so every known
wiki also has its own lookup that doesn't depend on them:
- MediaWiki (ArchWiki): title search through the wiki's API, by keyword;
- Hextra sites (Hyprland Wiki): the site's full-text search index, cached locally;
- anything else: page addresses from the sitemap, cached locally."""

import hashlib
import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from shellmate import library, library_sources, rag, web


# a question with one of these is (also) about the shell: its manual is consulted
SHELL_WORDS = ("shell", "оболочк", "alias", "алиас", "abbr", "prompt", "промпт", "path", "переменн", "variable",
               "env", "function", "функци", "script", "скрипт", "completion", "автодополн", "history", "истори")


@dataclass(frozen=True)
class Site:
    name: str
    domain: str
    keywords: tuple[str, ...] = ()  # only consulted when the question mentions one; empty = always
    mediawiki: bool = False  # clean article HTML via action=render + API title search
    index: str = ""  # path of a Hextra full-text search index (no page fetching needed)
    wikipedia: bool = False  # full-text search + plain-text extracts through the Wikipedia API
    offline_only: bool = False  # only in the offline library (library.py): never looked up online


KNOWN = {
    "wiki.archlinux.org": Site("ArchWiki", "wiki.archlinux.org", mediawiki=True),
    "wiki.hypr.land": Site("Hyprland Wiki", "wiki.hypr.land", ("hypr",), index="/en.search-data.json"),
    "wiki.cachyos.org": Site("CachyOS Wiki", "wiki.cachyos.org", ("cachy",)),
    "wiki.debian.org": Site("Debian Wiki", "wiki.debian.org"),
    "documentation.ubuntu.com": Site("Ubuntu Docs", "documentation.ubuntu.com"),
    "help.ubuntu.com": Site("Ubuntu Help", "help.ubuntu.com"),
    "docs.fedoraproject.org": Site("Fedora Docs", "docs.fedoraproject.org"),
    # general knowledge: its own search beats web engines for facts, and costs no search credits
    "en.wikipedia.org": Site("Wikipedia", "en.wikipedia.org", wikipedia=True),
    # offline library only (library.py): commands and the user's shell
    library_sources.TLDR_DOMAIN: Site("tldr", library_sources.TLDR_DOMAIN, offline_only=True),
    "www.gnu.org/software/bash": Site("Bash manual", "www.gnu.org/software/bash", ("bash",) + SHELL_WORDS, offline_only=True),
    "fishshell.com": Site("fish manual", "fishshell.com", ("fish",) + SHELL_WORDS, offline_only=True),
    "zsh.sourceforge.io": Site("zsh manual", "zsh.sourceforge.io", ("zsh",) + SHELL_WORDS, offline_only=True),
    "www.debian.org": Site("Debian Reference", "www.debian.org", offline_only=True),
}
WIKIPEDIA = "en.wikipedia.org"
# per distribution: its own docs first; ArchWiki is the best general Linux reference anywhere
# (its pacman-specific lines are filtered out on other distros by evidence.off_topic)
FAMILY_DOMAINS = {
    "arch": ["wiki.archlinux.org", "wiki.hypr.land", "wiki.cachyos.org"],
    "debian": ["wiki.debian.org", "wiki.archlinux.org", "wiki.hypr.land"],
    "fedora": ["docs.fedoraproject.org", "wiki.archlinux.org", "wiki.hypr.land"],
}
UBUNTU_DOMAINS = ["documentation.ubuntu.com", "help.ubuntu.com", "wiki.debian.org", "wiki.archlinux.org",
                  "wiki.hypr.land"]


def default_domains() -> list[str]:
    from shellmate import backends
    d = backends.distro()
    base = UBUNTU_DOMAINS if "ubuntu" in d.ids else FAMILY_DOMAINS.get(d.family, ["wiki.archlinux.org", "wiki.hypr.land"])
    # what only the offline library has: the user's shell manual, tldr, Debian Reference
    return base + [k for k, s in KNOWN.items() if s.offline_only and library.has(k)]

CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "shellmate" / "wikis"
MAX_AGE = 3 * 86400

# questions where documentation beats news: how-to, errors, configuration, "why doesn't X work"
HOWTO = re.compile(r"\b(как(?!\w)|почему|зачем|что такое|что значит|ошибк|не работа|настро|включ|выключ|"
                   r"установ|удал|исправ|почин|чинить|очист|how|why|what is|error|fail|config|setup|"
                   r"enable|disable|install|fix|broken|not working|clean)", re.I)
NEWSY = re.compile(r"(новост|пишут|говорят|мнени|обзор|вышл|выйдет|релиз|цена|стоит|news|review|"
                   r"release date|price|announc)", re.I)
STOP = set("how to the a an in on of for and or with my me is are do does can i you it this that "
           "what why when where which use using enable disable configure configuration config setup "
           "set command commands fix way best difference differences between versus vs compare comparison "
           "stand".split())


def is_howto(text: str) -> bool:
    return bool(HOWTO.search(text)) and not NEWSY.search(text)


def is_news(text: str) -> bool:
    return bool(NEWSY.search(text))


def sites_for(text: str, domains: list[str]) -> list[Site]:
    low = text.lower()
    sites = [KNOWN.get(d) or Site(d, d) for d in domains]
    return [s for s in sites if not s.keywords or any(k in low for k in s.keywords)]


def is_trusted(url: str, domains: list[str]) -> bool:
    """A domain may carry a path ("github.com/tldr-pages"): then the URL must start with it."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    where = f"{host}{parts.path}"
    for d in domains:
        d = d.lower().removeprefix("www.")
        if "/" in d and (where == d or where.startswith(d.rstrip("/") + "/")):
            return True
        if host == d or host.endswith("." + d):
            return True
    return False


def keywords(query_en: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+.-]*", query_en.lower())
    return list(dict.fromkeys(w for w in words if w not in STOP and len(w) > 1))


def _score(text: str, words: list[str]) -> int:
    low = text.lower()
    return sum(w[:5] in low for w in words)


def _cached(domain: str, name: str, fetch) -> str | None:
    """Fresh copy if possible, otherwise the last good one (works offline / when rate-limited)."""
    path = CACHE / f"{domain}-{name}"
    if path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
        return path.read_text()
    try:
        body = fetch()
        if body:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            return body
    except Exception:
        pass
    return path.read_text() if path.exists() else None


# --- per-site lookups that don't need a search engine --------------------------------------


def _index_chunks(site: Site) -> tuple[list[tuple[str, str]], dict[str, str]]:
    raw = _cached(site.domain, "search.json", lambda: web.get(f"https://{site.domain}{site.index}", 20)[1])
    try:
        pages = json.loads(raw or "{}")
    except ValueError:
        return [], {}
    items, titles = [], {}
    for path, page in pages.items():
        for key, text in (page.get("data") or {}).items():
            anchor, _, heading = key.partition("#")
            url = f"https://{site.domain}{path}" + (f"#{anchor}" if anchor else "")
            titles[url] = f"{site.name}: {page.get('title', '')}" + (f" › {heading}" if heading else "")
            section = f"§ {page.get('title', '')}" + (f" › {heading}" if heading else "")
            items += [(url, c) for c in rag.chunks(f"{section}\n\n{text}")]
    return items, titles


# ArchWiki keeps translations as "Title (Русский)"; English pages are the maintained ones
TRANSLATION = re.compile(r" \((?:Русский|Українська|Italiano|Español|Français|Deutsch|Português|Polski|"
                         r"Česky|Magyar|Dansk|Suomi|Nederlands|Svenska|Türkçe|Ελληνικά|Български|Српски|"
                         r"Slovenský|Hrvatski|Lietuvių|Bahasa Indonesia|简体中文|正體中文|日本語|한국어|"
                         r"العربية|עברית|ไทย)\)")


def _mediawiki_api(domain: str, **params) -> dict:
    url = f"https://{domain}/api.php?" + urllib.parse.urlencode({**params, "format": "json"})
    return json.loads(web.get(url)[1])


def _mediawiki_titles(domain: str, words: list[str]) -> list[str]:
    found: dict[str, int] = {}
    for w in words[:4]:
        try:
            hits = _mediawiki_api(domain, action="query", list="search", srsearch=w, srwhat="title",
                                  srlimit=10)["query"]["search"]
        except (OSError, ValueError, KeyError):
            continue
        for r in hits:
            if not TRANSLATION.search(r["title"]):
                found[r["title"]] = _score(r["title"], words)
    ranked = [t for t in sorted(found, key=lambda t: (-found[t], len(t))) if found[t]][:8]
    if not ranked:
        return []
    try:  # "Clean Cache" is a redirect to "Pacman#Cleaning the package cache": resolve to the article
        q = _mediawiki_api(domain, action="query", titles="|".join(ranked), redirects=1)["query"]
        target = {r["from"]: r["to"] for r in q.get("redirects", [])}
        ranked = list(dict.fromkeys(target.get(t, t) for t in ranked))
    except (OSError, ValueError, KeyError):
        pass
    return ranked


# --- Wikipedia ------------------------------------------------------------------------------------

WIKI_SKIP = re.compile(r"^(?:References|Notes|Citations|Sources|Bibliography|External links|See also|"
                       r"Further reading|Footnotes|Works cited)$", re.I)


def _wikipedia_api(**params) -> dict:
    """Wikimedia asks programs to name themselves (anonymous browser-like agents get rate-limited
    first) and to not repeat requests: answers are cached for three days."""
    from shellmate import updates
    query = urllib.parse.urlencode({**params, "format": "json"})
    agent = f"shellmate/{updates.version()} (https://github.com/radeonchik/shellmate; terminal assistant)"
    key = hashlib.sha1(query.encode()).hexdigest()[:16]
    body = _cached(WIKIPEDIA, f"api-{key}.json",
                   lambda: web.get(f"https://{WIKIPEDIA}/w/api.php?{query}", 10, headers={"User-Agent": agent})[1])
    if body is None:
        raise OSError("Wikipedia did not answer")
    return json.loads(body)


def _wikipedia_pages(query_en: str, n: int) -> list[web.Result]:
    """Full-text search ranks articles by relevance ("Pushkin birth and death years" -> Alexander Pushkin)."""
    # the whole phrase and its meaningful words both miss sometimes ("why is the sky blue" -> Diffuse sky
    # radiation, its words -> the colour Sky blue; "what does DNA stand for" -> a TV series, its words -> DNA):
    # the best article of each. The search wants every word in an article: fewer words if none matches.
    words = keywords(query_en)
    rankings = []
    for variants in ([query_en], [" ".join(words), " ".join(words[:2])]):
        for terms in variants:
            if not terms:
                continue
            try:
                hits = _wikipedia_api(action="query", list="search", srsearch=terms, srlimit=n + 2)["query"]["search"]
            except (OSError, ValueError, KeyError):
                hits = []
            hits = [h["title"] for h in hits if not h["title"].startswith("List of")
                    and "(disambiguation)" not in h["title"]]
            if hits:
                rankings.append(hits)
                break
    titles = list(dict.fromkeys(t for row in zip(*rankings) for t in row) if len(rankings) > 1
                  else (rankings[0] if rankings else []))[:n]
    return [web.Result(t, f"https://{WIKIPEDIA}/wiki/{urllib.parse.quote(t.replace(' ', '_'))}") for t in titles]


def _wikipedia_text(title: str) -> str:
    """The article as plain text, "== Section ==" turned into the "§ Title › Section" lines rag.chunks keeps."""
    try:
        pages = _wikipedia_api(action="query", prop="extracts", explaintext=1, exsectionformat="wiki",
                               titles=title, redirects=1)["query"]["pages"]
        text = next(iter(pages.values())).get("extract", "")
    except (OSError, ValueError, KeyError, StopIteration):
        return ""
    out, path, skip = [], [title], False
    for line in text.splitlines():
        m = re.fullmatch(r"(=+)\s*(.*?)\s*=+", line.strip())
        if m:
            level = len(m.group(1)) - 1  # "== A ==" is level 1, under the article title
            path = path[:level] + [m.group(2)]
            skip = any(WIKI_SKIP.match(p) for p in path[1:])
            out.append("\n§ " + " › ".join(path) + "\n")
        elif not skip:
            out.append(line)
    # pronunciations are dropped from extracts, leaving "Deoxyribonucleic acid (; DNA) is…"
    text = re.sub(r"\(\s*[;,]\s*", "(", "\n".join(out))
    return re.sub(r"\s*\(\s*\)", "", text)


def _sitemap_urls(domain: str) -> list[str]:
    def fetch() -> str:
        for path in ("/sitemap.xml", "/sitemap-index.xml"):
            try:
                body = web.get(f"https://{domain}{path}", 15)[1]
            except Exception:
                continue
            if "<sitemapindex" in body:  # Astro/Starlight style: an index of sitemaps
                body = "\n".join(web.get(loc, 15)[1] for loc in re.findall(r"<loc>([^<]+)</loc>", body))
            if "<loc>" in body:
                return "\n".join(re.findall(r"<loc>([^<]+)</loc>", body))
        return ""
    urls = (_cached(domain, "sitemap.txt", fetch) or "").split()
    # skip translations ("/ru/…") and old versioned copies ("/0.48.0/…")
    return [u for u in urls if not re.match(r"https?://[^/]+/(?:[a-z]{2}(?:-[a-z]{2})?|\d+\.\d+[\d.]*)/", u)
            or "/en/" in u]


def _local_pages(site: Site, words: list[str], n: int) -> list[web.Result]:
    if site.mediawiki:
        return [web.Result(t, f"https://{site.domain}/title/{urllib.parse.quote(t.replace(' ', '_'))}")
                for t in _mediawiki_titles(site.domain, words)[:n]]
    scored = [(u, _score(urllib.parse.unquote(u).replace("-", " ").replace("_", " "), words))
              for u in _sitemap_urls(site.domain)]
    best = sorted((s for s in scored if s[1]), key=lambda s: (-s[1], len(s[0])))[:n]
    return [web.Result(u.rstrip("/").rsplit("/", 1)[-1].replace("-", " "), u) for u, _ in best]


def _page_text(r: web.Result, site: Site) -> str:
    if site.wikipedia:
        return _wikipedia_text(r.title)
    if site.mediawiki:
        m = re.search(r"/title/([^?#]+)", r.url)
        if m:  # article body only, keeps code blocks as lines (better than any extracted text)
            body = web.fetch_text(f"https://{site.domain}/index.php?title={m.group(1)}&action=render")
            if body:  # render can fail on the wiki's side (ArchWiki answered 502 in Oct 2026): the full page
                return body
    return r.text or web.fetch_text(r.url)


# --- the entry point -------------------------------------------------------------------------------


def gather(question: str, query_en: str, domains: list[str], searx_url: str = "",
           pages_per_site: int = 2, shape="") -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Ranked chunks from the favored wikis that are relevant to the question, plus titles.
    shape: a sketch of the answer for ranking (rag.ANSWER_SHAPE_TASK), or a Future of one that is
    still being written while the pages download."""
    words = keywords(query_en) or keywords(question)
    query = f"{question} {query_en}"
    per_site: dict[Site, list[tuple[str, str]]] = {}
    titles: dict[str, str] = {}
    fetch: list[tuple[str, Site]] = []
    leads: list[tuple[str, str]] = []
    for site in sites_for(query, domains):
        if library.has(site.domain):  # downloaded: milliseconds, no network
            items, t = library.search(site.domain, f"{query_en} {question}", pages=3)
            if items:
                per_site[site], titles = items, {**titles, **t}
            continue
        if site.offline_only:
            continue
        if site.index:
            items, t = _index_chunks(site)
            per_site[site], titles = items, {**titles, **t}
            continue
        if site.wikipedia:
            for r in _wikipedia_pages(query_en or question, pages_per_site):
                fetch.append((r, site))
                titles[r.url] = f"Wikipedia: {r.title}"
            continue
        try:  # a search engine knows relevance best — when it isn't rate-limited
            results = [r for r in web.search(query_en, searx_url, n=6, domains=[site.domain])
                       if is_trusted(r.url, [site.domain])]
        except web.WebError:
            results = []
        seen = {r.url for r in results}
        results += [r for r in _local_pages(site, words, pages_per_site) if r.url not in seen]
        for r in results[:pages_per_site]:
            fetch.append((r, site))
            titles[r.url] = f"{site.name}: {r.title.split(' - ')[0]}"
    if fetch:
        with ThreadPoolExecutor(max_workers=len(fetch)) as ex:
            texts = list(ex.map(lambda f: _page_text(*f), fetch))
        for (r, site), text in zip(fetch, texts):
            parts = rag.chunks(text)
            per_site.setdefault(site, []).extend((r.url, c) for c in parts)
            if site.wikipedia and parts:  # the lead is the article's summary: dates, definitions, who and what
                leads.append((r.url, parts[0]))
    # rank within each site, then topic-specific wikis (Hyprland for a Hyprland question) go
    # before the general one, so ArchWiki's KDE section doesn't win a Hyprland question
    ordered = sorted((s for s in per_site if per_site[s]), key=lambda s: not s.keywords)
    k = 4 if len(ordered) > 1 else 6
    shape = shape.result() if hasattr(shape, "result") else shape
    items = [it for s in ordered for it in rag.rank_many([query, shape], per_site[s])[:k]]
    return list(dict.fromkeys(leads + items)), titles
