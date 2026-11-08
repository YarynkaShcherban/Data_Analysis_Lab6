"""
Конвертація .ipynb у компактний PDF із гарною якістю.

Запуск:
    python nb2pdf.py ноутбук.ipynb                 # -> ноутбук.pdf
    python nb2pdf.py ноутбук.ipynb звіт.pdf        # своє ім'я
    python nb2pdf.py ноутбук.ipynb --max-width 1000 --quality 80

Потрібно один раз:
    pip install nbconvert nbformat pillow playwright
    playwright install chromium

Що робить:
  1. nbconvert перетворює ноутбук на HTML (текст, таблиці, виводи лишаються як є).
  2. Усі вбудовані картинки (виводи і вставки у markdown) зменшуються до --max-width
     пікселів і перекодовуються: фото/графіки в JPEG, а картинки з малою кількістю
     кольорів (скріншоти таблиць, схеми) лишаються PNG, але стискаються палітрою.
  3. Інтерактивні карти folium/leaflet у PDF не відображаються (це iframe на
     кілька МБ), тому замість них вставляється підпис; сама карта лишається у .html.
  4. Chromium друкує HTML у PDF: кирилиця, таблиці та рядки коду коректні.
  5. Якщо в системі є Ghostscript, PDF додатково стискається.
"""
import argparse
import base64
import io
import os
import re
import shutil
import subprocess
import sys

import nbformat
from nbconvert import HTMLExporter
from PIL import Image

CSS = """
<style>
  @page { size: A4; margin: 14mm 12mm; }
  body { background: #fff !important; }
  .jp-Notebook, .container { max-width: 100% !important; width: 100% !important; padding: 0 !important; }
  div.jp-Cell, .jp-RenderedHTMLCommon { page-break-inside: auto; }
  .jp-OutputArea-output, .jp-RenderedImage, img { max-width: 100% !important; height: auto !important; }
  .jp-OutputArea-child, .jp-Cell-inputWrapper, .jp-Cell-outputWrapper { page-break-inside: avoid; }
  pre, code { white-space: pre-wrap !important; word-break: break-word !important; font-size: 9.5pt !important; }
  table { font-size: 8.5pt !important; display: block; overflow: hidden; max-width: 100%; }
  .jp-InputPrompt, .jp-OutputPrompt { min-width: 0 !important; width: 0 !important; flex: 0 0 0 !important;
                                      visibility: hidden; overflow: hidden; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
</style>
"""

FOLIUM_NOTE = ("<div style='border:1px dashed #888;padding:8px;color:#555;font:12px sans-serif'>"
               "Інтерактивна карта (folium) не відображається у PDF. Див. файл lr5_маршрут.html.</div>")


def drop_interactive_maps(nb):
    """Прибрати з виводів важкі інтерактивні карти, лишивши текстове представлення."""
    n = 0
    for cell in nb.cells:
        if cell.cell_type != 'code':
            continue
        for out in cell.get('outputs', []):
            data = out.get('data')
            if not data or 'text/html' not in data:
                continue
            html = ''.join(data['text/html'])
            if len(html) > 50_000 and re.search(r'leaflet|folium', html, re.I):
                data['text/html'] = FOLIUM_NOTE
                data.pop('text/plain', None)
                n += 1
    return n


def recompress(match, max_w, quality):
    mime, b64 = match.group(1), match.group(2)
    raw = base64.b64decode(b64)
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return match.group(0)

    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)

    has_alpha = im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info)
    rgb = im.convert('RGBA' if has_alpha else 'RGB')

    # скільки різних кольорів: мало -> схема/скріншот, краще PNG із палітрою
    sample = rgb.convert('RGB').resize((min(rgb.width, 300), max(1, round(rgb.height * min(rgb.width, 300) / rgb.width))))
    few_colors = len(sample.getcolors(maxcolors=100000) or range(10 ** 6)) < 2000

    buf = io.BytesIO()
    if few_colors or has_alpha:
        out = rgb.quantize(colors=128, method=Image.FASTOCTREE) if not has_alpha else rgb
        out.save(buf, 'PNG', optimize=True)
        new_mime = 'image/png'
    else:
        rgb.save(buf, 'JPEG', quality=quality, optimize=True, progressive=True)
        new_mime = 'image/jpeg'

    new = buf.getvalue()
    if len(new) >= len(raw):                       # не роздувати
        return match.group(0)
    return 'data:%s;base64,%s' % (new_mime, base64.b64encode(new).decode())


def shrink_images(html, max_w, quality):
    before = len(html)
    html = re.sub(r'data:(image/(?:png|jpeg|jpg));base64,([A-Za-z0-9+/=\s]+)',
                  lambda m: recompress(m, max_w, quality), html)
    return html, before - len(html)


def html_to_pdf(html_path, pdf_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.goto('file://' + os.path.abspath(html_path), wait_until='load')
        page.emulate_media(media='print')
        page.pdf(path=pdf_path, format='A4', print_background=True, prefer_css_page_size=True)
        b.close()


def gs_compress(pdf_path):
    gs = shutil.which('gs') or shutil.which('gswin64c') or shutil.which('gswin32c')
    if not gs:
        return False
    tmp = pdf_path + '.gs.pdf'
    # /printer = 300 dpi; /ebook = 150 dpi. Беремо ebook: текст лишається векторним
    r = subprocess.run([gs, '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.5', '-dPDFSETTINGS=/ebook',
                        '-dNOPAUSE', '-dQUIET', '-dBATCH', '-dAutoRotatePages=/None',
                        '-sOutputFile=' + tmp, pdf_path], capture_output=True)
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) < os.path.getsize(pdf_path):
        os.replace(tmp, pdf_path)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def main():
    ap = argparse.ArgumentParser(description='ipynb -> компактний PDF')
    ap.add_argument('notebook')
    ap.add_argument('pdf', nargs='?')
    ap.add_argument('--max-width', type=int, default=1100, help='макс. ширина картинок, px (за замовч. 1100)')
    ap.add_argument('--quality', type=int, default=82, help='якість JPEG 1-95 (за замовч. 82)')
    ap.add_argument('--keep-html', action='store_true', help='залишити проміжний .html')
    a = ap.parse_args()

    pdf = a.pdf or os.path.splitext(a.notebook)[0] + '.pdf'
    html_path = os.path.splitext(pdf)[0] + '._tmp.html'

    nb = nbformat.read(a.notebook, as_version=4)
    dropped = drop_interactive_maps(nb)

    body, _ = HTMLExporter(template_name='lab').from_notebook_node(nb)
    body = body.replace('</head>', CSS + '</head>', 1)
    body, saved = shrink_images(body, a.max_width, a.quality)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(body)

    html_to_pdf(html_path, pdf)
    gs_used = gs_compress(pdf)
    if not a.keep_html:
        os.remove(html_path)

    print('готово: %s (%.2f МБ)' % (pdf, os.path.getsize(pdf) / 1e6))
    print('  інтерактивних карт замінено підписом: %d' % dropped)
    print('  зекономлено на картинках: %.2f МБ' % (saved / 1e6))
    print('  Ghostscript: %s' % ('застосовано' if gs_used else 'не знайдено або не потрібен'))


if __name__ == '__main__':
    sys.exit(main())
