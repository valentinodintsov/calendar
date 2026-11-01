# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Сборка PDF-версий документации: Markdown -> HTML -> PDF (headless Chromium).

Запуск из корня репозитория:
    .venv/bin/pip install markdown          # один раз
    .venv/bin/python docs/build_pdfs.py     # все PDF
    .venv/bin/python docs/build_pdfs.py API.pdf "Инструкция пользователя.pdf"   # только перечисленные

Нужен Chromium (chromium-browser) и шрифты PT Astra Serif, Liberation Sans, DejaVu Sans Mono.
Два оформления: справочное (синие заголовки) и «Инструкция пользователя» — с обложкой и номерами страниц.
"""
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import markdown
except ImportError:
    sys.exit("Нужен пакет markdown: .venv/bin/pip install markdown")

DOCS = Path(__file__).resolve().parent
REPO = DOCS.parent
CHROMIUM = shutil.which("chromium-browser") or shutil.which("chromium") or shutil.which("google-chrome")


def slugify(value, separator="-"):
    """Якоря как на GitHub: «## 1. Вход и выход» -> «1-вход-и-выход»."""
    value = re.sub(r"<[^>]+>", "", html.unescape(value)).strip().lower()
    value = re.sub(r"[^\w\- ]", "", value)
    return value.replace(" ", separator)


def md_to_html(text):
    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
        extension_configs={"toc": {"slugify": slugify}},
        output_format="html5",
    )


CLASSIC_CSS = """
@page { size: A4; margin: 18mm 17mm 18mm 17mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: "PT Astra Serif", "Liberation Serif", serif; font-size: 11pt; line-height: 1.45; color: #1a1a1a; }
h1, h2, h3, h4 { font-family: "Liberation Sans", Arial, sans-serif; line-height: 1.25; page-break-after: avoid; break-after: avoid; }
h1 { font-size: 20pt; margin: 0 0 10pt; color: #1a1a1a; }
h2 { font-size: 14pt; color: #1f4e79; border-bottom: 1.2pt solid #1f4e79; padding-bottom: 3pt; margin: 18pt 0 8pt; }
h3 { font-size: 12pt; color: #1f4e79; margin: 14pt 0 6pt; }
h4 { font-size: 11pt; margin: 12pt 0 4pt; }
p { margin: 0 0 7pt; }
a { color: #1f4e79; text-decoration: none; }
ul, ol { margin: 0 0 7pt; padding-left: 20pt; }
li { margin: 2pt 0; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.6pt; background: #f1f3f5; border-radius: 2pt; padding: 0.5pt 2.5pt; }
pre { background: #f6f8fa; border: 0.6pt solid #d0d7de; border-left: 3pt solid #1f4e79; border-radius: 3pt; padding: 6pt 8pt; overflow: hidden; white-space: pre-wrap; word-break: break-word; page-break-inside: avoid; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8.2pt; line-height: 1.35; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 9pt; font-size: 9.6pt; }
th, td { border: 0.6pt solid #b8c4d0; padding: 3.5pt 5pt; vertical-align: top; text-align: left; }
th { background: #e8eef4; font-family: "Liberation Sans", Arial, sans-serif; font-size: 9pt; }
tr { page-break-inside: avoid; break-inside: avoid; }
blockquote { margin: 6pt 0 9pt; padding: 5pt 9pt; border-left: 3pt solid #1f4e79; background: #f3f6f9; }
blockquote p:last-child { margin-bottom: 0; }
hr { border: 0; border-top: 0.6pt solid #b8c4d0; margin: 12pt 0; }
"""

DESIGNED_CSS = """
@page { size: A4; margin: 20mm 18mm 18mm 18mm;
  @bottom-left { content: "Календарь. Инструкция пользователя"; font: 8pt "Liberation Sans", Arial, sans-serif; color: #333; }
  @bottom-right { content: counter(page) " / " counter(pages); font: 8pt "Liberation Sans", Arial, sans-serif; color: #333; }
}
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: "Liberation Sans", Arial, sans-serif; font-size: 10.5pt; line-height: 1.5; color: #1f1f22; }
.cover { height: 245mm; display: flex; flex-direction: column; justify-content: center; page-break-after: always; break-after: page; }
.cover .kicker { color: #c2410c; letter-spacing: .08em; text-transform: uppercase; font-size: 11pt; margin-bottom: 18pt; }
.cover h1 { font-size: 30pt; margin: 0 0 22pt; border: 0; }
.cover .lead { font-size: 11pt; max-width: 120mm; color: #333; }
.cover .copy { margin-top: 60pt; font-size: 9pt; color: #777; }
.toc-page { page-break-after: always; break-after: page; }
h1, h2, h3 { line-height: 1.25; page-break-after: avoid; break-after: avoid; }
h2 { font-size: 15pt; border-bottom: 1pt solid #c2410c; padding-bottom: 5pt; margin: 26pt 0 10pt; }
h3 { font-size: 12pt; margin: 16pt 0 6pt; }
p { margin: 0 0 7pt; }
a { color: inherit; text-decoration: none; }
ul, ol { margin: 0 0 8pt; padding-left: 20pt; }
li { margin: 2.5pt 0; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.6pt; background: #f2f0ec; border-radius: 2pt; padding: 0.5pt 3pt; }
pre { background: #f7f5f1; border: 0.6pt solid #ddd6cc; border-radius: 3pt; padding: 6pt 8pt; white-space: pre-wrap; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 10pt; font-size: 9.6pt; }
th, td { border: 0.6pt solid #c9c3b8; padding: 5pt 6pt; vertical-align: top; text-align: left; }
th { background: #efebe4; }
tr { page-break-inside: avoid; break-inside: avoid; }
hr { display: none; }
"""


def page(title, body, css):
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{css}</style></head><body>{body}</body></html>"""


def print_pdf(html_text, out):
    if not CHROMIUM:
        sys.exit("Не найден Chromium (chromium-browser)")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "doc.html"
        src.write_text(html_text, encoding="utf-8")
        subprocess.run(
            [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", f"--user-data-dir={Path(tmp) / 'profile'}",
             "--no-pdf-header-footer", f"--print-to-pdf={out}", src.as_uri()],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
        )
    print("готово:", Path(out).relative_to(REPO))


def classic(md_path, out):
    text = Path(md_path).read_text(encoding="utf-8")
    title = text.splitlines()[0].lstrip("# ").strip()
    print_pdf(page(title, md_to_html(text), CLASSIC_CSS), out)


def designed_user_guide(md_path, out):
    text = Path(md_path).read_text(encoding="utf-8")
    lines = text.splitlines()
    title_line, rest = lines[0], "\n".join(lines[1:])
    # вступление — до «## Содержание», оглавление — до первого «---»
    intro, _, rest = rest.partition("## Содержание")
    toc, _, body = rest.partition("\n---\n")
    cover = f"""<section class="cover"><div class="kicker">Инструкция пользователя</div><h1>Календарь</h1>
<div class="lead">{md_to_html(intro.strip())}</div><div class="copy">© Валентин Одинцов, 2026</div></section>"""
    toc_html = f'<section class="toc-page"><h2>Содержание</h2>{md_to_html(toc.strip())}</section>'
    print_pdf(page("Календарь. Инструкция пользователя", cover + toc_html + md_to_html(body), DESIGNED_CSS), out)


# PDF -> (исходный Markdown, оформление)
TARGETS = {
    "USER_GUIDE.pdf": (DOCS / "USER_GUIDE.md", classic),
    "API.pdf": (DOCS / "API.md", classic),
    "ADMIN_GUIDE.pdf": (DOCS / "ADMIN_GUIDE.md", classic),
    "INTEGRATIONS.pdf": (DOCS / "INTEGRATIONS.md", classic),
    "DIRECTORY.pdf": (DOCS / "DIRECTORY.md", classic),
    "README.pdf": (REPO / "main" / "README.md", classic),
    "docker.zs.int - инструкция администратора.pdf": (DOCS / "DOCKER_ZS_INT.md", classic),
    "Инструкция пользователя.pdf": (DOCS / "USER_GUIDE.md", designed_user_guide),
}

if __name__ == "__main__":
    names = sys.argv[1:] or list(TARGETS)
    unknown = [n for n in names if n not in TARGETS]
    if unknown:
        sys.exit("Неизвестные файлы: " + ", ".join(unknown) + "\nДоступны: " + ", ".join(TARGETS))
    for name in names:
        source, build = TARGETS[name]
        build(source, DOCS / name)
