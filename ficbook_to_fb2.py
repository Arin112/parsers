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

import tkinter as tk
from bs4 import BeautifulSoup, NavigableString, Tag
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from tkinter import filedialog, messagebox, ttk
from webdriver_manager.chrome import ChromeDriverManager

SETTINGS_PATH = Path(__file__).with_name("settings.ini")
CHROME_PROFILE_DIR = Path(__file__).with_name("chrome_profile")
REQUEST_DELAY_SECONDS = 3
PAGE_WAIT_SECONDS = 25
CAPTCHA_POLL_SECONDS = 5
CAPTCHA_MAX_WAIT_SECONDS = 60 * 30


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
        with self.path.open("w", encoding="utf-8") as file:
            self.config.write(file)


class FicbookParser:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def parse(self, url: str) -> FicData:
        driver = self._create_driver()
        try:
            self.logger.info("Открываем страницу произведения в браузере: %s", url)
            self._open_with_possible_captcha(driver, url)
            self._wait_for(driver, (By.CSS_SELECTOR, "ul.list-of-fanfic-parts"))

            soup = BeautifulSoup(driver.page_source, "html.parser")
            title = self._extract_title(soup)
            author = self._extract_author(soup)
            annotation = self._extract_annotation(soup)
            chapter_urls = self._extract_chapter_urls(soup, base_url=url)

            if not chapter_urls:
                raise RuntimeError("Не удалось найти список глав")

            self.logger.info("Найдено глав: %s", len(chapter_urls))
            chapters: List[Chapter] = []

            for index, chapter_url in enumerate(chapter_urls, start=1):
                if index > 1:
                    self.logger.info("Пауза %s секунды перед следующей главой", REQUEST_DELAY_SECONDS)
                    time.sleep(REQUEST_DELAY_SECONDS)

                self.logger.info("Открываем главу %s/%s: %s", index, len(chapter_urls), chapter_url)
                self._open_with_possible_captcha(driver, chapter_url)
                self._wait_for(driver, (By.CSS_SELECTOR, "#content"))

                chapter_soup = BeautifulSoup(driver.page_source, "html.parser")
                chapters.append(self._parse_chapter(chapter_soup))

            return FicData(
                title=title,
                author=author,
                annotation=annotation,
                source_url=url,
                chapters=chapters,
            )
        finally:
            self.logger.info("Закрываем браузер Selenium")
            driver.quit()

    def _create_driver(self) -> webdriver.Chrome:
        CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        options = Options()
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--lang=ru-RU")
        options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
        options.add_argument("--profile-directory=Default")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
        return driver

    def _open_with_possible_captcha(self, driver: webdriver.Chrome, target_url: str) -> None:
        driver.get(target_url)

        if self._looks_like_target_page(driver.current_url, target_url):
            return

        self.logger.warning(
            "Похоже на редирект (возможна Cloudflare/капча). "
            "Ожидаю ручное прохождение. Текущий URL: %s",
            driver.current_url,
        )
        self.logger.info(
            "После прохождения капчи вернитесь на вкладку браузера. "
            "Скрипт продолжит работу автоматически."
        )

        started = time.time()
        while time.time() - started < CAPTCHA_MAX_WAIT_SECONDS:
            current_url = driver.current_url
            if self._looks_like_target_page(current_url, target_url):
                self.logger.info("Доступ восстановлен, продолжаю парсинг.")
                return

            self.logger.info("Ожидаю решение капчи... Текущий URL: %s", current_url)
            time.sleep(CAPTCHA_POLL_SECONDS)

        raise RuntimeError(
            "Превышено время ожидания прохождения капчи "
            f"({CAPTCHA_MAX_WAIT_SECONDS} сек)."
        )

    def _looks_like_target_page(self, current_url: str, target_url: str) -> bool:
        current = normalize_readfic_url(current_url)
        target = normalize_readfic_url(target_url)
        current_parsed = urlparse(current)
        target_parsed = urlparse(target)

        if current_parsed.netloc != target_parsed.netloc:
            return False
        if "/readfic/" not in current_parsed.path:
            return False
        if "cdn-cgi" in current_parsed.path:
            return False
        return True

    def _wait_for(self, driver: webdriver.Chrome, locator: tuple[str, str]) -> None:
        WebDriverWait(driver, PAGE_WAIT_SECONDS).until(EC.presence_of_element_located(locator))

    def _extract_title(self, soup: BeautifulSoup) -> str:
        for selector in ('[itemprop="name"]', "h1", "title"):
            tag = soup.select_one(selector)
            if tag and tag.get_text(strip=True):
                return tag.get_text(" ", strip=True)
        return "Без названия"

    def _extract_author(self, soup: BeautifulSoup) -> str:
        for tag in soup.select('a[href*="/authors/"]'):
            text = tag.get_text(" ", strip=True)
            if text:
                return text
        return "Неизвестный автор"

    def _extract_annotation(self, soup: BeautifulSoup) -> Optional[str]:
        for selector in (".summary_text", "[itemprop='description']", ".fanfic-description"):
            tag = soup.select_one(selector)
            if tag:
                text = tag.get_text("\n", strip=True)
                if text:
                    return text
        return None

    def _extract_chapter_urls(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        urls: List[str] = []
        seen = set()
        for link in soup.select("ul.list-of-fanfic-parts a.part-link"):
            href = link.get("href")
            if not href:
                continue
            full_url = urljoin(base_url, href.split("#")[0])
            if full_url not in seen:
                seen.add(full_url)
                urls.append(full_url)
        return urls

    def _parse_chapter(self, soup: BeautifulSoup) -> Chapter:
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
            annotation_block = "<annotation>" + "\n".join(
                f"<p>{escape(line)}</p>" for line in fic.annotation.splitlines() if line.strip()
            ) + "</annotation>"

        sections = []
        for chapter in fic.chapters:
            date_line = ""
            if chapter.date_iso:
                date_line = f"<p><emphasis>Дата публикации: {escape(chapter.date_iso)}</emphasis></p>"

            note_block = ""
            if chapter.note_text:
                note_block = "<subtitle>Примечания</subtitle>\n" + "\n".join(
                    f"<p>{escape(line)}</p>" for line in chapter.note_text.splitlines() if line.strip()
                )

            sections.append(
                "\n".join(
                    [
                        "<section>",
                        f"  <title><p>{escape(chapter.title)}</p></title>",
                        f"  {date_line}" if date_line else "",
                        f"  {chapter.body_html}",
                        f"  {note_block}" if note_block else "",
                        "</section>",
                    ]
                )
            )

        return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<FictionBook xmlns=\"http://www.gribuser.ru/xml/fictionbook/2.0\" xmlns:l=\"http://www.w3.org/1999/xlink\">
  <description>
    <title-info>
      <genre>fanfic</genre>
      <author><nickname>{escape(fic.author)}</nickname></author>
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
{chr(10).join('    ' + section for section in sections)}
  </body>
</FictionBook>
"""


class TextHandler(logging.Handler):
    def __init__(self, widget: tk.Text):
        super().__init__()
        self.widget = widget

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)

        def append() -> None:
            self.widget.configure(state="normal")
            self.widget.insert(tk.END, message + "\n")
            self.widget.see(tk.END)
            self.widget.configure(state="disabled")

        self.widget.after(0, append)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Ficbook → FB2 (Selenium)")
        self.root.geometry("780x440")

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
        ttk.Button(frame, text="Выбрать…", command=self._choose_dir).grid(row=3, column=1, padx=(8, 0), sticky="ew")

        self.parse_btn = ttk.Button(frame, text="Спарсить", command=self._start_parse)
        self.parse_btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 8))

        self.log_text = tk.Text(frame, height=15, state="disabled")
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
        threading.Thread(target=self._parse_worker, args=(url, out_dir), daemon=True).start()

    def _parse_worker(self, url: str, out_dir: str) -> None:
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            self.settings.set_output_dir(out_dir)

            parser = FicbookParser(self.logger)
            fic = parser.parse(url)
            fb2_xml = FB2Builder.build(fic)

            target = Path(out_dir) / self._make_filename(fic.title)
            target.write_text(fb2_xml, encoding="utf-8")
            self.logger.info("Готово: %s", target)
            self.root.after(0, lambda: messagebox.showinfo("Готово", f"FB2 сохранён:\n{target}"))
        except Exception as exc:
            self.logger.exception("Ошибка парсинга: %s", exc)
            self.root.after(0, lambda: messagebox.showerror("Ошибка", str(exc)))
        finally:
            self.root.after(0, lambda: self.parse_btn.configure(state="normal"))

    @staticmethod
    def _make_filename(title: str) -> str:
        clean = re.sub(r"[\\/:*?\"<>|]+", "_", title).strip(" .")
        return f"{clean or 'book'}.fb2"


def normalize_readfic_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
