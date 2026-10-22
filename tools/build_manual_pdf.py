"""Build docs/manual/stlToSolid-User-Manual.pdf from the Markdown manual.

    uv run --with markdown --with playwright python tools/build_manual_pdf.py

The Markdown is turned into one HTML page (images read from docs/manual/img)
and printed to A4 with headless Chromium. Each numbered section starts on a
new page; the links in the contents table keep working in the PDF.
"""
import asyncio
import pathlib
import re

import markdown
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANUAL = ROOT / 'docs' / 'manual'
SRC = MANUAL / 'stlToSolid-User-Manual.md'
OUT = MANUAL / 'stlToSolid-User-Manual.pdf'


def slugify(value, separator='-'):
    # GitHub's anchors: lower case, punctuation dropped, each space a hyphen
    value = re.sub(r'[^\w\- ]', '', value.strip().lower())
    return value.replace(' ', separator)


CSS = """
@page { size: A4; margin: 18mm 17mm 20mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font: 10.5pt/1.5 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #1d2229; margin: 0; }
h1 { font-size: 26pt; margin: 6mm 0 2mm; letter-spacing: -0.01em; }
h2 { font-size: 17pt; margin: 0 0 4mm; padding-bottom: 2mm; border-bottom: 2px solid #2bb3cf; break-before: page; }
h3 { font-size: 12.5pt; margin: 6mm 0 2mm; break-after: avoid; }
p, li { orphans: 3; widows: 3; }
a { color: #137f96; text-decoration: none; }
code { font: 9pt "SF Mono", Menlo, monospace; background: #eef3f6; padding: 0 3px; border-radius: 3px; }
table { width: 100%; border-collapse: collapse; margin: 3mm 0 4mm; font-size: 9.2pt; break-inside: auto; }
tr { break-inside: avoid; }
th { text-align: left; background: #eaf4f7; }
th, td { border: 1px solid #cfd9e0; padding: 4px 6px; vertical-align: top; }
img { max-width: 100%; display: block; margin: 3mm auto 1mm; border-radius: 4px; break-inside: avoid; }
p:has(> img) { break-inside: avoid; }
/* the settings crops are a narrow rail: keep them at a readable size */
img[src*="set-"], img[src*="report-"], img[src*="fusion-dialog"] { max-width: 62mm; }
img[src*="report-solid"] { max-width: 70mm; }
img[src*="fusion-button"] { max-width: 90mm; }
em { color: #4b5663; }
p:has(> img) > em { display: block; text-align: center; font-size: 9pt; margin: 1mm 0 4mm; }
blockquote { margin: 3mm 0; padding: 2mm 4mm; border-left: 3px solid #2bb3cf; background: #f2f8fa; }
blockquote p { margin: 1mm 0; }
hr { display: none; }
.cover { text-align: center; padding-top: 55mm; }
.cover img { max-width: 110mm; margin-bottom: 8mm; }
"""


def build_html():
    md = SRC.read_text(encoding='utf-8')
    body = markdown.markdown(
        md, extensions=['tables', 'toc', 'attr_list', 'sane_lists'],
        extension_configs={'toc': {'slugify': slugify}})
    # the logo, title and version lines become the cover page
    body = body.replace('<p align="center">\n  <img', '<div class="cover"><p><img', 1)
    body = body.replace('October 2026</p>', 'October 2026</p></div>', 1)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>stlToSolid User Manual</title>'
            f'<base href="{MANUAL.as_uri()}/"><style>{CSS}</style></head>'
            f'<body>{body}</body></html>')


async def main():
    html = MANUAL / '.manual-print.html'
    html.write_text(build_html(), encoding='utf-8')
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(html.as_uri(), wait_until='networkidle')
            await page.pdf(
                path=str(OUT), format='A4', print_background=True,
                display_header_footer=True,
                header_template='<span></span>',
                footer_template=(
                    '<div style="font:8pt -apple-system,Arial;color:#8b95a3;width:100%;'
                    'padding:0 17mm;display:flex;justify-content:space-between">'
                    '<span>stlToSolid user manual · v0.5.7</span>'
                    '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'),
                margin={'top': '18mm', 'bottom': '20mm', 'left': '17mm', 'right': '17mm'})
            await browser.close()
    finally:
        html.unlink(missing_ok=True)
    print('wrote', OUT)


if __name__ == '__main__':
    asyncio.run(main())
