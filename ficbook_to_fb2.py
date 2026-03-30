import configparser
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
import tkinter as tk
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tkinter import filedialog, messagebox, ttk


SETTINGS_PATH = Path(__file__).with_name("settings.ini")
REQUEST_DELAY_SECONDS = 3
TIMEOUT_SECONDS = 20
REQUEST_RETRIES = 5
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


@dataclass
class Chapter:
    title: str
    date_iso: Optional[str]
    body_html: str
    note_text: Optional[str]


@dataclass
class FicData:
    title: str
    author: str
    annotation: Optional[str]
    source_url: str
    chapters: List[Chapter]


class SettingsManager:
    def __init__(self, path: Path):
        self.path = path
        self.config = configparser.ConfigParser()
        if self.path.exists():
            self.config.read(self.path, encoding="utf-8")

    def get_output_dir(self) -> str:
        return self.config.get("app", "output_dir", fallback=str(Path.home()))

    def set_output_dir(self, value: str) -> None:
        if "app" not in self.config:
            self.config["app"] = {}
        self.config["app"]["output_dir"] = value
        with self.path.open("w", encoding="utf-8") as f:
            self.config.write(f)


class FicbookParser:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        retry = Retry(
            total=REQUEST_RETRIES,
            connect=REQUEST_RETRIES,
            read=REQUEST_RETRIES,
            status=REQUEST_RETRIES,
            backoff_factor=1.2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def parse(self, url: str) -> FicData:
        self.logger.info("Запрос страницы произведения: %s", url)
        self._warmup_session(url)
        root_html = self._get(url, referer="https://ficbook.net/")
        soup = BeautifulSoup(root_html, "html.parser")

        title = self._extract_title(soup)
        author = self._extract_author(soup)
        annotation = self._extract_annotation(soup)
        chapter_urls = self._extract_chapter_urls(soup, base_url=url)

        if not chapter_urls:
            raise RuntimeError("Не удалось найти список глав.")

        self.logger.info("Найдено глав: %s", len(chapter_urls))
        chapters: List[Chapter] = []

        for idx, chapter_url in enumerate(chapter_urls, start=1):
            if idx > 1:
                self.logger.info("Пауза %s секунды перед следующей главой", REQUEST_DELAY_SECONDS)
                time.sleep(REQUEST_DELAY_SECONDS)

            self.logger.info("Чтение главы %s/%s: %s", idx, len(chapter_urls), chapter_url)
            chapter_html = self._get(chapter_url, referer=url)
            chapter = self._parse_chapter(chapter_html)
            chapters.append(chapter)

        return FicData(
            title=title,
            author=author,
            annotation=annotation,
            source_url=url,
            chapters=chapters,
        )

    def _warmup_session(self, work_url: str) -> None:
        host = f"{urlparse(work_url).scheme}://{urlparse(work_url).netloc}"
        try:
            self.logger.info("Прогрев сессии: %s", host)
            self.session.get(host, timeout=TIMEOUT_SECONDS)
        except Exception as exc:
            self.logger.warning("Не удалось прогреть сессию: %s", exc)

    def _get(self, url: str, referer: Optional[str] = None) -> str:
        headers = {}
        if referer:
            headers["Referer"] = referer

        last_exc: Optional[Exception] = None
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                self.logger.info("HTTP GET %s (попытка %s/%s)", url, attempt, REQUEST_RETRIES)
                response = self.session.get(url, timeout=TIMEOUT_SECONDS, headers=headers)
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                last_exc = exc
                self.logger.warning("Ошибка запроса %s (попытка %s/%s): %s", url, attempt, REQUEST_RETRIES, exc)
                if attempt < REQUEST_RETRIES:
                    sleep_for = 2 * attempt
                    self.logger.info("Ожидание %s сек. перед повтором", sleep_for)
                    time.sleep(sleep_for)

        raise RuntimeError(f"Не удалось загрузить страницу после {REQUEST_RETRIES} попыток: {url}") from last_exc

    def _extract_title(self, soup: BeautifulSoup) -> str:
        selectors = [
            '[itemprop="name"]',
            "h1",
            "title",
        ]
        for selector in selectors:
            tag = soup.select_one(selector)
            if tag and tag.get_text(strip=True):
                return tag.get_text(" ", strip=True)
        return "Без названия"

    def _extract_author(self, soup: BeautifulSoup) -> str:
        candidates = soup.select('a[href*="/authors/"]')
        for tag in candidates:
            text = tag.get_text(" ", strip=True)
            if text:
                return text
        return "Неизвестный автор"

    def _extract_annotation(self, soup: BeautifulSoup) -> Optional[str]:
        for selector in [".summary_text", "[itemprop='description']", ".fanfic-description"]:
            tag = soup.select_one(selector)
            if tag:
                text = tag.get_text("\n", strip=True)
                if text:
                    return text
        return None

    def _extract_chapter_urls(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        urls = []
        seen = set()
        for link in soup.select("ul.list-of-fanfic-parts a.part-link"):
            href = link.get("href")
            if not href:
                continue
            full = urljoin(base_url, href.split("#")[0])
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

    def _parse_chapter(self, html: str) -> Chapter:
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.select_one("h2[itemprop='headline']") or soup.select_one("h2")
        title = title_tag.get_text(" ", strip=True) if title_tag else "Без названия главы"

        date_tag = soup.select_one(".part-date[itemprop='datePublished']")
        date_iso = date_tag.get("content") if date_tag else None

        content_tag = soup.select_one("#content")
        if not content_tag:
            raise RuntimeError(f"Не найден текст главы: {title}")

        body_html = self._content_to_fb2_markup(content_tag)

        note_tag = soup.select_one(".part-comment-bottom .urlized-links")
        note_text = note_tag.get_text("\n", strip=True) if note_tag else None

        return Chapter(title=title, date_iso=date_iso, body_html=body_html, note_text=note_text)

    def _content_to_fb2_markup(self, content_tag: Tag) -> str:
        paragraphs: List[str] = []

        for node in content_tag.children:
            if isinstance(node, NavigableString):
                text = str(node).strip()
                if text:
                    for part in re.split(r"\n\s*\n", text):
                        part = part.strip()
                        if part:
                            paragraphs.append(f"<p>{escape(part)}</p>")
                continue

            if not isinstance(node, Tag):
                continue

            if node.name == "p":
                paragraphs.append(f"<p>{self._inline_to_fb2(node)}</p>")
            else:
                rendered = self._inline_to_fb2(node).strip()
                if rendered:
                    paragraphs.append(f"<p>{rendered}</p>")

        return "\n".join(paragraphs)

    def _inline_to_fb2(self, node: Tag) -> str:
        chunks: List[str] = []

        for child in node.children:
            if isinstance(child, NavigableString):
                chunks.append(escape(str(child)))
                continue

            if not isinstance(child, Tag):
                continue

            inner = self._inline_to_fb2(child)
            if child.name in {"i", "em"}:
                chunks.append(f"<emphasis>{inner}</emphasis>")
            elif child.name in {"b", "strong"}:
                chunks.append(f"<strong>{inner}</strong>")
            elif child.name == "br":
                chunks.append("<empty-line/>")
            else:
                chunks.append(inner)

        return "".join(chunks)


class FB2Builder:
    @staticmethod
    def build(fic: FicData) -> str:
        now_iso = datetime.now(timezone.utc).date().isoformat()
        book_id = f"ficbook-{abs(hash((fic.title, fic.source_url))) % (10 ** 12)}"

        annotation_block = ""
        if fic.annotation:
            annotation_paragraphs = "\n".join(
                f"<p>{escape(line)}</p>" for line in fic.annotation.splitlines() if line.strip()
            )
            annotation_block = f"<annotation>{annotation_paragraphs}</annotation>"

        sections = []
        for chapter in fic.chapters:
            date_line = f"<p><emphasis>Дата публикации: {escape(chapter.date_iso)}</emphasis></p>" if chapter.date_iso else ""
            note_line = ""
            if chapter.note_text:
                note_paragraphs = "\n".join(
                    f"<p>{escape(line)}</p>" for line in chapter.note_text.splitlines() if line.strip()
                )
                note_line = f"<subtitle>Примечания</subtitle>\n{note_paragraphs}"

            section = f"""
<section>
  <title><p>{escape(chapter.title)}</p></title>
  {date_line}
  {chapter.body_html}
  {note_line}
</section>
""".strip()
            sections.append(section)

        return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<FictionBook xmlns=\"http://www.gribuser.ru/xml/fictionbook/2.0\" xmlns:l=\"http://www.w3.org/1999/xlink\">
  <description>
    <title-info>
      <genre>fanfic</genre>
      <author>
        <nickname>{escape(fic.author)}</nickname>
      </author>
      <book-title>{escape(fic.title)}</book-title>
      {annotation_block}
      <lang>ru</lang>
    </title-info>
    <document-info>
      <author><nickname>ficbook_to_fb2</nickname></author>
      <program-used>ficbook_to_fb2.py</program-used>
      <date value=\"{now_iso}\">{now_iso}</date>
      <id>{book_id}</id>
      <version>1.0</version>
      <src-url>{escape(fic.source_url)}</src-url>
    </document-info>
  </description>
  <body>
    {''.join(sections)}
  </body>
</FictionBook>
"""


class TextHandler(logging.Handler):
    def __init__(self, widget: tk.Text):
        super().__init__()
        self.widget = widget

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)

        def append() -> None:
            self.widget.configure(state="normal")
            self.widget.insert(tk.END, msg + "\n")
            self.widget.see(tk.END)
            self.widget.configure(state="disabled")

        self.widget.after(0, append)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Ficbook → FB2")
        self.root.geometry("760x420")

        self.settings = SettingsManager(SETTINGS_PATH)
        self.logger = logging.getLogger("ficbook_to_fb2")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()

        self.url_var = tk.StringVar()
        self.out_var = tk.StringVar(value=self.settings.get_output_dir())

        self._build_ui()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="URL произведения:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.url_var).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="Папка для FB2:").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.out_var).grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(frame, text="Выбрать…", command=self._choose_dir).grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(0, 8))

        self.parse_btn = ttk.Button(frame, text="Спарсить", command=self._start_parse)
        self.parse_btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self.log_text = tk.Text(frame, height=14, state="disabled")
        self.log_text.grid(row=5, column=0, columnspan=2, sticky="nsew")

        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=0)
        frame.rowconfigure(5, weight=1)

        handler = TextHandler(self.log_text)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(handler)

    def _choose_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.home()))
        if selected:
            self.out_var.set(selected)

    def _start_parse(self) -> None:
        url = normalize_readfic_url(self.url_var.get().strip())
        out_dir = self.out_var.get().strip()

        if not url:
            messagebox.showerror("Ошибка", "Введите URL")
            return

        if not out_dir:
            messagebox.showerror("Ошибка", "Выберите папку для сохранения")
            return

        self.parse_btn.configure(state="disabled")
        thread = threading.Thread(target=self._parse_worker, args=(url, out_dir), daemon=True)
        thread.start()

    def _parse_worker(self, url: str, out_dir: str) -> None:
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            self.settings.set_output_dir(out_dir)

            parser = FicbookParser(self.logger)
            fic = parser.parse(url)

            fb2_xml = FB2Builder.build(fic)
            filename = self._make_filename(fic.title)
            target = Path(out_dir) / filename

            target.write_text(fb2_xml, encoding="utf-8")
            self.logger.info("Готово: %s", target)
            self.root.after(0, lambda: messagebox.showinfo("Готово", f"FB2 сохранен:\n{target}"))
        except Exception as exc:
            self.logger.exception("Ошибка парсинга: %s", exc)
            self.root.after(0, lambda: messagebox.showerror("Ошибка", str(exc)))
        finally:
            self.root.after(0, lambda: self.parse_btn.configure(state="normal"))

    @staticmethod
    def _make_filename(title: str) -> str:
        clean = re.sub(r"[\\/:*?\"<>|]+", "_", title).strip(" .")
        clean = clean or "book"
        return f"{clean}.fb2"


def normalize_readfic_url(url: str) -> str:
    parsed = urlparse(url)
    cleaned = parsed._replace(query="", fragment="")
    return cleaned.geturl()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
