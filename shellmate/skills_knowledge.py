"""Intent handlers that answer questions from the web or the user's own documents.
Mixed into cli.App, so `self` has say/phrase/ask_model/cfg."""

import json
import re
import urllib.parse
from pathlib import Path

from shellmate import config, docs, evidence, llm, rag, trusted, ui, web
from shellmate.prompts import lang_of


def _cyrillic(text: str) -> bool:
    return bool(re.search(r"[а-яёіїєґ]", text, re.I))


class KnowledgeSkills:
    @property
    def trusted_domains(self) -> list[str]:
        return self.cfg.trusted_sites or trusted.default_domains()

    def _answer(self, query: str, items: list[tuple[str, str]], origin: str,
                titles: dict[str, str] | None = None, quiet_empty: bool = False, hint: str = "") -> bool:
        """Extractive answer (see evidence.py): sentences are shortlisted and topic-checked by code,
        the model only labels each one, and what the user sees are those sentences verbatim.
        Returns whether an answer was found; quiet_empty lets the caller fall back silently."""
        question = rag.core_question(query)
        model = self.model_for()
        with ui.working("проверяю, что из найденного действительно отвечает на вопрос…"):
            ev = evidence.pick(items, question, hint, self.cfg.ollama_url, model)
        # second opinion when the fast model hesitated over what counts as an answer. ("Found nothing"
        # is NOT a trigger: measured, the big model then also finds nothing — it only added latency.)
        if ev.answers and ev.confidence < self.cfg.escalate_quotes and self.big and model != self.big:
            with ui.working(f"не уверена в цитатах ({ev.confidence:.0%}) — перепроверяю ({self.big})…"):
                ev = evidence.pick(items, question, hint, self.cfg.ollama_url, self.big)
        if not ev.answers:
            if not quiet_empty:
                self.say(ui.yellow(self.phrase("no_evidence")))
                if ev.related:
                    self._show(ev.related, query, titles, "Может пригодиться — по теме, но не прямой ответ",
                               "source")
            return False
        if self.cfg.answer_mode == "summary":
            quotes = "\n".join(f"- {s.text.split(chr(10))[0]}" for s in ev.answers)
            self.ask_model(rag.SUMMARY_TASK.format(lang=lang_of(query), quotes=quotes), query, title="Кратко")
        else:
            self.say(self.phrase("quotes"))
        official = all(trusted.is_trusted(s.src, self.trusted_domains) for s in ev.answers)
        self._show(ev.answers + ev.related, query, titles,
                   "Из официальной документации" if official else "Цитаты", "quote", related=len(ev.answers))
        return True

    def _show(self, sents: list, query: str, titles: dict[str, str] | None, title: str, color: str,
              related: int | None = None) -> None:
        """related: index from which sentences are 'on topic, not a direct answer' — shown dimmer."""
        sources = list(dict.fromkeys(s.src for s in sents))
        texts = [s.text.split("\n", 1)[0] for s in sents]  # prose is translated, commands are not
        shown = texts
        if any(_cyrillic(t) != _cyrillic(query) for t in texts):
            with ui.working("перевожу цитаты…"):
                shown = self._translate(texts, lang_of(query)) or texts
        lines = []
        for n, (s, original, text) in enumerate(zip(sents, texts, shown)):
            where = f"  {ui.dim('— ' + s.heading)}" if s.heading else ""
            if related is not None and n == related:
                lines.append(ui.dim("ещё по теме:"))
            body = f"«{text}»" if related is None or n < related else ui.dim(f"«{text}»")
            lines.append(f"{ui.rgb(color, ui.sup(sources.index(s.src) + 1))} {body}{where}")
            if text != original:
                lines.append(f"  {ui.dim(ui.italic(original))}")
            for c in s.text.split("\n")[1:]:
                if c[:2] in ("$ ", "# "):  # ArchWiki marks commands: "$ " as user, "# " as root
                    lines.append(f"  {ui.dim(c[0])} {ui.bold(c[2:])}")
                else:  # file contents / config: shown as a code block, not as something to run
                    lines.append(f"  {ui.rgb('accent', '▏')} {ui.bold(c)}")
        ui.panel(title, lines, color, "❝")
        ui.panel("Источники", [self._source_line(i, src, (titles or {}).get(src, ""),
                                                 trusted.is_trusted(src, self.trusted_domains))
                               for i, src in enumerate(sources, 1)], "source", "§")

    @staticmethod
    def _source_line(i: int, src: str, title: str, official: bool = False) -> str:
        if src.startswith(("http://", "https://")):
            url = urllib.parse.urlsplit(src)
            where = url.netloc.removeprefix("www.") + (url.path if len(url.path) < 40 else url.path[:39] + "…")
            star = ui.rgb("accent2", " ★ официальная вики") if official else ""
            return (f"{ui.rgb('quote', ui.sup(i))} {ui.bold(title or url.netloc)}{star}  "
                    f"{ui.dim(ui.link(where, src))}")
        path = Path(src)
        return (f"{ui.rgb('quote', ui.sup(i))} {ui.bold(path.name)}  "
                f"{ui.dim(ui.link(docs.short(str(path.parent)), path.as_uri()))}")

    def _translate(self, texts: list[str], lang: str) -> list[str] | None:
        raw = llm.chat(self.cfg.ollama_url, self.model_for(),
                       [{"role": "system", "content": rag.TRANSLATE_TASK.format(lang=lang)},
                        {"role": "user", "content": json.dumps(texts, ensure_ascii=False)}],
                       schema=rag.translate_schema(len(texts)))
        try:
            out = json.loads(raw)["t"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if len(out) != len(texts) or not all(isinstance(t, str) and t for t in out):
            return None
        # small models sometimes "translate" by copying: then show originals only, not twice
        want_cyr = lang in ("Russian", "Ukrainian")
        return [t if _cyrillic(t) == want_cyr else o for t, o in zip(out, texts)]

    def from_trusted(self, question: str, query_en: str) -> bool:
        """Favored wikis first. True if they answered (quotes already printed)."""
        with ui.working("ищу в официальных вики…"):
            items, titles = trusted.gather(question, query_en, self.trusted_domains, self.cfg.searx_url)
        if items and self._answer(question, items, "excerpts of OFFICIAL documentation wikis", titles, quiet_empty=True,
                                  hint=query_en):
            return True
        self.say(ui.dim("в официальных вики прямого ответа нет"))
        return False

    def web(self, it, query: str) -> int:
        q = it.text or query
        if trusted.is_howto(query) and self.from_trusted(query, q):
            return 0
        return 0 if self.from_web(query, q, loud=True) else 1

    def from_web(self, question: str, q: str, loud: bool = False) -> bool:
        """Open web search. loud: report 'nothing found' to the user instead of failing silently."""
        try:
            with ui.working(f"ищу в интернете: {q[:80]}"):
                results = web.search_many([rag.core_question(question), q], self.cfg.searx_url)
        except web.WebError as e:
            if loud:
                ui.problem(str(e), title="Поиск недоступен")
            return False
        if not results:
            if loud:
                self.say(ui.yellow(self.phrase("not_found", q=q)))
            return False
        with ui.working(f"читаю {min(4, len(results))} страницы…"):
            items = web.gather(f"{question} {q}", results)
        return self._answer(question, items, "excerpts of web pages found by a search engine just now",
                            {r.url: r.title for r in results}, quiet_empty=not loud, hint=q)

    # --- local documents ----------------------------------------------------------

    def _reindex(self) -> None:
        with ui.working("проверяю документы…") as sp:
            n, total, empty = docs.reindex(
                self.cfg.name, self.cfg.docs,
                lambda i, t, p: sp.update(f"индексирую {i}/{t}: {p.name}"))
        if n:
            self.say(ui.dim(f"обновил индекс: {n} файл(ов), всего {total}"))
        if empty:
            ui.panel(f"{len(empty)} PDF без текстового слоя — это сканы, прочитать их я пока не могу",
                     [ui.dim(docs.short(p)) for p in empty[:5]], "warn", "!")

    def docs(self, it, query: str) -> int:
        if it.action == "add":
            return self.docs_add(it.text)
        if it.action == "remove":
            return self.docs_remove(it.text)
        if not self.cfg.docs:
            self.say(f"источников пока нет. Добавь папку, например: "
                     f"{ui.bold(self.cfg.name + ' добавь ~/Documents в источники')}")
            return 1
        self._reindex()
        with ui.working("ищу в документах…"):
            hits = docs.search(self.cfg.name, it.text or query)
        if not hits:
            self.say(ui.yellow(self.phrase("not_found", q=it.text or query)))
            return 1
        self._answer(query, hits, "excerpts from the USER'S OWN documents (their local files, "
                     "which they asked you to search)")
        return 0

    def docs_add(self, folder: str) -> int:
        path = Path(folder or ".").expanduser().resolve()
        if not path.is_dir():
            ui.problem(f"папки {folder} нет")
            return 1
        if str(path) not in self.cfg.docs:
            self.cfg.docs.append(str(path))
            config.save(self.cfg)
        self._reindex()
        files, chunks = docs.stats(self.cfg.name)
        self.say(ui.green(f"{docs.short(str(path))} добавлена. В индексе {files} файлов ({chunks} фрагментов)."))
        return 0

    def docs_remove(self, folder: str) -> int:
        path = str(Path(folder).expanduser().resolve()) if folder else ""
        if path not in self.cfg.docs:
            ui.panel("Папки-источники", [docs.short(d) for d in self.cfg.docs] or [ui.dim("нет")], "source", "§")
            return 1
        self.cfg.docs.remove(path)
        config.save(self.cfg)
        self._reindex()
        self.say(self.phrase("done"))
        return 0
