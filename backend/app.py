import os
import re
import io
import json
import base64
import zipfile
import concurrent.futures

import requests
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin, urlparse
from PIL import Image as PILImage

# apimart / Gemini 支持的输出比例
SUPPORTED_RATIOS = {
    '1:1':  1.0,
    '3:4':  3 / 4,
    '4:3':  4 / 3,
    '9:16': 9 / 16,
    '16:9': 16 / 9,
}

def best_aspect_ratio(width: int, height: int) -> str:
    """根据图片宽高返回最接近的支持比例。"""
    if width <= 0 or height <= 0:
        return '1:1'
    ratio = width / height
    best_name = '1:1'
    best_diff = float('inf')
    for name, r in SUPPORTED_RATIOS.items():
        diff = abs(ratio - r)
        if diff < best_diff:
            best_diff = diff
            best_name = name
    return best_name

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB
CORS(app, resources={r"/*": {"origins": "*"}},
     allow_headers=["Content-Type"],
     methods=["GET", "POST", "OPTIONS"])

# ── 提示词（全部存放后端，前端不接触） ──────────────────────────
COLORING_PROMPT = (
    "IMPORTANT: You MUST faithfully reproduce the EXACT same character, subject, "
    "pose, and composition from the provided reference image. Do NOT invent new "
    "elements, backgrounds, or subjects that are not in the reference. "
    "(UNCOLORED) minimalist contour drawing coloring page based on the reference "
    "image. Maintain exact composition and proportions. Style: simple cartoon "
    "vector outline, very clean. Key requirement: Simplify all details heavily. "
    "Use uniform medium-thick black outlines throughout. Strictly pure black lines "
    "on a pure white background. Closed strokes ready for filling. MONOCHROME ONLY. "
    "CRITICAL NEGATIVE CONSTRAINTS - STRICT ADHERENCE REQUIRED: ABSOLUTELY NO "
    "COLORS, NO COLORED LINES, NO COLORED FILLS, and NO COLORED ELEMENTS of any "
    "kind. NO shading, NO gradients, NO gray tones, NO grayscale, NO cross-hatching, "
    "NO stippling, and NO dot patterns. NO solid black fill areas anywhere. NO "
    "realism or photographic details; stick to flat 2D line art. NO background "
    "textures, grain, or paper effects. NO transparent or semi-transparent pixels; "
    "only 100% opaque black and 100% opaque white. NO double border lines; ensure "
    "all lines are single, clean contour strokes. Furthermore, ensure absolutely "
    "NO fine texture lines that could be interpreted as shading or depth. All areas "
    "must be rendered as completely flat, open, and empty shapes surrounded by clean "
    "contour lines. The final output must be strictly two-dimensional with zero "
    "suggestion of shadow, texture, or color volume. NO sketching style, NO messy "
    "scratchy lines, NO broken strokes, NO hatching lines, NO noise, NO jagged "
    "edges, NO intricate internal details. Do NOT add any new background scenery, "
    "landscapes, or objects not present in the original reference image."
)

RENAME_PROMPT_TEMPLATE = """请帮我重新命名以下 {count} 张填色页图片。
每个图片命名的格式例子如下：
[中][29][{theme}][{theme} Listening To Music With Headphones]

规则：
1. 第一个[]中的内容是难度描述，一共有三个难度，分别为低，中，高。你需要根据画面难度去选中一个难度匹配。画面难度的区分可以参考：画面需要填色的多少，线条的复杂程度，需要填色的精细程度来确定
2. 第二个[]中的内容是序号，请将所有的序号都列为0，但是你必须选出一张第一个[]难度是低的图片，将序号改为1
3. 第三个[]中的内容是主题词，这个主题词所有的图片都使用: {theme}
4. 第四个[]中的内容是标题，这里的每张图片的标题都必须包含主题词 "{theme}"。标题的生成根据图片的画面来生成，需要标题6-8个英文单词即可，表述简单易懂。标题最好联想这个主题词有关的故事，比如动漫或者游戏人物可以联想剧情什么的，画面可以包括表情，动作，服装等

请严格按照以下 JSON 格式返回，不要返回任何其他文字：
[
  {{"index": 0, "name": "[难度][序号][{theme}][标题]"}},
  {{"index": 1, "name": "[难度][序号][{theme}][标题]"}}
]
其中 index 是图片序号（0开始），name 是完整的命名字符串。"""


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route('/')
def serve_index():
    return jsonify({'status': 'ok'})

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


# ── 代理设置：云端留空，本地可通过环境变量 PROXY_URL 配置 ──
_proxy_url = os.environ.get("PROXY_URL", "").strip()
PROXIES = {"http": _proxy_url, "https": _proxy_url} if _proxy_url else None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# 常见的图片属性列表，用于提取懒加载图片
IMG_ATTRS = [
    'src', 'data-src', 'data-lazy-src', 'data-original',
    'data-lazy', 'data-url', 'data-image', 'data-hi-res-src',
    'data-real-src', 'data-fallback-src',
]

# 用于下载图片的 requests session
_session = requests.Session()
_session.headers.update(HEADERS)


# ── 工具函数 ─────────────────────────────────────────────────────
def do_request(url, stream=False, timeout=25):
    """用 requests 下载图片数据。"""
    parsed = urlparse(url)
    # 为目标域名设置对应的 Referer，避免防盗链拦截（Wikimedia 等）
    extra_headers = {
        'Referer': f"{parsed.scheme}://{parsed.netloc}/",
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    }
    kwargs = dict(timeout=timeout, stream=stream, allow_redirects=True,
                  headers=extra_headers)
    if PROXIES:
        try:
            r = _session.get(url, proxies=PROXIES, **kwargs)
            r.raise_for_status()
            return r
        except Exception:
            pass
    r = _session.get(url, **kwargs)
    r.raise_for_status()
    return r


def _try_mediawiki_api(url: str) -> list[dict] | None:
    """如果 URL 是 MediaWiki 站点（Fandom/Wikia 等），用 API 获取图片列表，绕过 Cloudflare。"""
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # 检测 Fandom / Wikia / 其他 MediaWiki 站点
    is_wiki = any(d in host for d in ['fandom.com', 'wikia.com', 'wikia.org',
                                       'wikipedia.org', 'wikimedia.org',
                                       'fextralife.com', 'wiki.gg'])
    if not is_wiki and '/wiki/' not in parsed.path:
        return None

    # 从 URL 提取页面名称
    path = parsed.path
    wiki_match = re.search(r'/wiki/(.+?)(?:\?|#|$)', path)
    if not wiki_match:
        return None
    page_name = wiki_match.group(1)

    # 构建 API URL：Wikipedia/Wikimedia 用 /w/api.php，Fandom 用 /api.php
    _wmf = any(d in host for d in ['wikipedia.org', 'wikimedia.org'])
    api_paths = ['/w/api.php', '/api.php'] if _wmf else ['/api.php', '/w/api.php']
    api_base = None
    for ap in api_paths:
        test_url = f"{parsed.scheme}://{parsed.netloc}{ap}"
        try:
            tr = _session.get(test_url, params={'action': 'query', 'meta': 'siteinfo',
                              'format': 'json'}, timeout=10)
            if tr.status_code == 200 and 'query' in tr.text[:500]:
                api_base = test_url
                break
        except Exception:
            continue
    if not api_base:
        return None

    try:
        resp = _session.get(api_base, params={
            'action': 'parse', 'page': page_name,
            'prop': 'text', 'format': 'json'
        }, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if 'parse' in data and 'text' in data['parse']:
            html = data['parse']['text']['*']
            images = _extract_images_from_html(html, url)
            if images:
                return images
    except Exception:
        pass

    # 备选：用 imageinfo API 获取图片直链
    try:
        resp = _session.get(api_base, params={
            'action': 'parse', 'page': page_name,
            'prop': 'images', 'format': 'json'
        }, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        filenames = data.get('parse', {}).get('images', [])
        if not filenames:
            return None

        images = []
        # 分批查询图片 URL（每次最多50个）
        for i in range(0, len(filenames), 50):
            batch = filenames[i:i+50]
            titles = '|'.join('File:' + f for f in batch)
            resp2 = _session.get(api_base, params={
                'action': 'query', 'titles': titles,
                'prop': 'imageinfo', 'iiprop': 'url',
                'format': 'json'
            }, timeout=20)
            resp2.raise_for_status()
            pages = resp2.json().get('query', {}).get('pages', {})
            for pid, pdata in pages.items():
                ii = pdata.get('imageinfo', [{}])
                img_url = ii[0].get('url', '') if ii else ''
                if img_url and re.search(r'\.(jpg|jpeg|png|gif|webp)(\?|$)', img_url, re.I):
                    alt = pdata.get('title', '').replace('File:', '').replace('_', ' ')
                    alt = re.sub(r'\.[a-zA-Z]{2,5}$', '', alt)
                    images.append({'src': img_url, 'alt': alt or 'image'})
        return images if images else None
    except Exception:
        return None


def _playwright_fetch(url: str) -> str:
    """在子进程中用 Playwright 获取页面 HTML（避免与 Flask 线程冲突）。"""
    import subprocess, sys
    script = f'''
import sys
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
ctx = browser.new_context(
    viewport={{"width":1920,"height":1080}},
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
page = ctx.new_page()
page.goto(sys.argv[1], wait_until="domcontentloaded", timeout=30000)
for _ in range(15):
    t = page.title()
    html_snippet = page.content()[:3000].lower()
    if "just a moment" not in t.lower() and "checking" not in t.lower() and "cf_chl" not in html_snippet:
        break
    page.wait_for_timeout(2000)
page.wait_for_timeout(3000)
page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
page.wait_for_timeout(2000)
page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
page.wait_for_timeout(1000)
html = page.content()
ctx.close()
browser.close()
pw.stop()
sys.stdout.buffer.write(html.encode("utf-8"))
'''
    result = subprocess.run(
        [sys.executable, '-c', script, url],
        capture_output=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[:500])
    return result.stdout.decode('utf-8')


def _is_cloudflare_page(html: str) -> bool:
    """检测 HTML 是否仍然是 Cloudflare 挑战页。"""
    if len(html) < 60000:
        lower = html[:5000].lower()
        if 'cf_chl' in html or '__cf_chl_tk' in html:
            return True
        if 'just a moment' in lower and 'cloudflare' in lower:
            return True
    return False


def clean_image_url(url: str) -> str:
    """清理图片 URL，去除缩略图参数、追踪参数等，获取高清原图。"""
    # Fandom / Wikia 缩略图参数
    url = re.sub(r'/revision/latest/scale-to-width-down/\d+', '/revision/latest', url)
    url = re.sub(r'/revision/latest/smart-width/\d+', '/revision/latest', url)
    url = re.sub(r'\?cb=\d+&(path-prefix=[^&]+&)?width=\d+.*$', '', url)
    url = re.sub(r'\?width=\d+.*$', '', url)
    # 去除常见追踪/缓存参数（cb, v, t, timestamp, _等）
    url = re.sub(r'[?&](cb|v|t|timestamp|_|nc|bust|cache|ver|version)=[^&]*', '', url)
    # 如果 ? 后面什么都没有了，去掉 ?
    url = re.sub(r'\?$', '', url)
    # 去除 URL 片段
    url = url.split('#')[0]
    return url


def sanitize_filename(name: str) -> str:
    """清理文件名：下划线转空格，移除非法字符。"""
    name = name.replace('_', ' ')
    name = re.sub(r'[\\/*?:"<>|]', '', name).strip()
    return name[:120] if name else 'image'


def get_ext(url: str, content_type: str = '') -> str:
    """根据 URL 或 Content-Type 推断图片扩展名。"""
    clean = url.split('?')[0].split('#')[0]
    m = re.search(r'\.(jpg|jpeg|png|gif|webp|svg|bmp|avif)$', clean, re.I)
    if m:
        return '.' + m.group(1).lower()
    if 'png' in content_type:  return '.png'
    if 'gif' in content_type:  return '.gif'
    if 'webp' in content_type: return '.webp'
    if 'svg' in content_type:  return '.svg'
    return '.jpg'


def best_from_srcset(srcset: str) -> str:
    """从 srcset 属性中提取最高分辨率的图片 URL。"""
    best_src, best_w = '', 0
    for part in srcset.split(','):
        tokens = part.strip().split()
        if not tokens:
            continue
        src = tokens[0]
        w = 1
        if len(tokens) > 1:
            try:
                w = int(tokens[1].rstrip('wx'))
            except ValueError:
                pass
        if w >= best_w:
            best_w, best_src = w, src
    return best_src


# ── 从 HTML 中提取图片 ──────────────────────────────────────────
def _extract_images_from_html(html: str, url: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    parsed_base = urlparse(url)
    seen: set[str] = set()
    images: list[dict] = []

    def make_absolute(src: str):
        if not src or src.startswith('data:'):
            return None
        if src.startswith('//'):
            return 'https:' + src
        if src.startswith('/'):
            return f"{parsed_base.scheme}://{parsed_base.netloc}{src}"
        if not src.startswith('http'):
            return urljoin(url, src)
        return src

    def add_image(src: str, alt: str):
        src = make_absolute(src)
        if not src:
            return
        if src.startswith('data:') and len(src) < 300:
            return
        # 过滤掉 wiki 文件页面 URL（如 /wiki/File:XXX.jpg），这些是 HTML 页面，不是直接图片
        if re.search(r'/wiki/(File|Special|Image):', src, re.I):
            return
        src = clean_image_url(src)
        if src in seen:
            return
        # 也用纯路径（不含 query）作为去重键，避免同一图片不同参数重复
        base_key = src.split('?')[0]
        if base_key in seen:
            return
        seen.add(src)
        seen.add(base_key)
        alt = (alt or '').replace('_', ' ').strip()
        # 如果没有 alt，从 URL 文件名生成
        if not alt:
            path_part = src.split('?')[0].split('#')[0]
            fname = path_part.rsplit('/', 1)[-1] if '/' in path_part else ''
            fname = re.sub(r'\.[a-zA-Z]{2,5}$', '', fname)
            alt = fname.replace('_', ' ').replace('-', ' ').strip()
        if not alt:
            alt = 'image'
        images.append({'src': src, 'alt': alt})

    for img in soup.find_all('img'):
        if not isinstance(img, Tag):
            continue
        src = None
        for attr in IMG_ATTRS:
            val = img.get(attr)
            if val and not str(val).startswith('data:image/gif;base64,R0lGOD'):
                src = str(val)
                break
        if not src:
            srcset = img.get('srcset') or img.get('data-srcset')
            if srcset:
                src = best_from_srcset(str(srcset))
        alt = str(img.get('alt') or img.get('title') or '')
        if src:
            add_image(src, alt)

    for source in soup.find_all('source'):
        if not isinstance(source, Tag):
            continue
        srcset = source.get('srcset') or source.get('data-srcset')
        if srcset:
            src = best_from_srcset(str(srcset))
            if src:
                add_image(src, 'picture_source')

    for video in soup.find_all('video', poster=True):
        if isinstance(video, Tag):
            add_image(str(video['poster']), 'video_poster')

    for a in soup.find_all('a', href=True):
        if not isinstance(a, Tag):
            continue
        href = str(a['href'])
        if re.search(r'\.(jpg|jpeg|png|gif|webp|svg|bmp)(\?|$)', href, re.I):
            add_image(href, str(a.get('title') or ''))

    for el in soup.find_all(style=True):
        if not isinstance(el, Tag):
            continue
        for m in re.finditer(r"url\(['\"]?(.*?)['\"]?\)", str(el.get('style', ''))):
            u = m.group(1)
            if re.search(r'\.(jpg|jpeg|png|gif|webp|svg|bmp)', u, re.I):
                add_image(u, 'bg_image')

    for m in re.finditer(
        r'https?://[^\s"\'<>]+?\.(jpg|jpeg|png|gif|webp|bmp|svg)(\?[^\s"\'<>]*)?',
        html, re.I
    ):
        add_image(m.group(0), 'found_image')

    for meta in soup.find_all('meta'):
        if not isinstance(meta, Tag):
            continue
        prop = str(meta.get('property') or meta.get('name') or '')
        if 'image' in prop.lower():
            content = str(meta.get('content') or '')
            if content.startswith('http'):
                add_image(content, 'meta_image')

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            obj = json.loads(script.string or '')
            _extract_json_images(obj, add_image)
        except Exception:
            pass

    return images


# ── 从页面中提取分页链接 ──────────────────────────────────────────
def _extract_pagination_links(html: str, base_url: str) -> list[str]:
    """从 HTML 中提取分页链接（下一页、第2/3/4页等）。"""
    soup = BeautifulSoup(html, 'html.parser')
    parsed = urlparse(base_url)
    links = []
    seen_urls = {base_url}

    def make_abs(href):
        if not href:
            return None
        if href.startswith('//'):
            return 'https:' + href
        if href.startswith('/'):
            return f"{parsed.scheme}://{parsed.netloc}{href}"
        if not href.startswith('http'):
            return urljoin(base_url, href)
        return href

    # 查找包含分页相关的链接
    pagination_selectors = [
        'a.next', 'a.pagination-next', 'a[rel="next"]',
        '.pagination a', '.pager a', '.page-numbers a',
        'nav a', '.nav-links a', '.paginator a',
        '.category-page__pagination a',          # Fandom 分类页
        '.category-page__pagination-next a',     # Fandom 下一页
    ]
    for sel in pagination_selectors:
        for a in soup.select(sel):
            href = a.get('href')
            full = make_abs(href)
            if full and full not in seen_urls and parsed.netloc in (urlparse(full).netloc):
                seen_urls.add(full)
                links.append(full)

    # 也查找文字为"下一页"/"Next"/">"的链接
    for a in soup.find_all('a', href=True):
        text = (a.get_text(strip=True) or '').lower()
        if text in ('next', '下一页', '下一页 »', '下一页 ›', '›', '»', '>', '>>', 'next →', 'next page', '次のページ'):
            full = make_abs(a['href'])
            if full and full not in seen_urls and parsed.netloc in (urlparse(full).netloc):
                seen_urls.add(full)
                links.append(full)

    return links


# ── /scrape ──────────────────────────────────────────────────────
@app.route('/scrape', methods=['POST', 'OPTIONS'])
def scrape():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    deep = data.get('deep', False)    # 是否自动爬取分页
    max_pages = min(int(data.get('maxPages', 10)), 50)  # 最多爬多少页

    if not url:
        return jsonify({'error': 'URL 为空'}), 400

    # 对于 wiki 类站点，优先用 MediaWiki API 获取图片直链
    parsed_url = urlparse(url)
    _host = parsed_url.netloc.lower()
    _is_wiki_site = any(d in _host for d in [
        'fandom.com', 'wikia.com', 'wikia.org',
        'wikipedia.org', 'wikimedia.org',
        'fextralife.com', 'wiki.gg'
    ]) or '/wiki/' in parsed_url.path

    if _is_wiki_site:
        wiki_images = _try_mediawiki_api(url)
        if wiki_images:
            return jsonify(wiki_images)

    # 用 requests 快速抓取
    html = None
    try:
        r = _session.get(url, timeout=15, allow_redirects=True)
        r.raise_for_status()
        if 'just a moment' not in r.text[:2000].lower():
            html = r.text
    except Exception:
        pass

    if html:
        images = _extract_images_from_html(html, url)
        # 深度抓取：自动跟踪分页链接
        if deep and images:
            visited = {url}
            next_links = _extract_pagination_links(html, url)
            page_num = 1
            while next_links and page_num < max_pages:
                next_url = next_links.pop(0)
                if next_url in visited:
                    continue
                visited.add(next_url)
                page_num += 1
                try:
                    r2 = _session.get(next_url, timeout=15, allow_redirects=True)
                    r2.raise_for_status()
                    if 'just a moment' in r2.text[:2000].lower():
                        break
                    page_images = _extract_images_from_html(r2.text, next_url)
                    # 全局去重
                    existing = {img['src'] for img in images}
                    for img in page_images:
                        if img['src'] not in existing:
                            images.append(img)
                            existing.add(img['src'])
                    # 继续查找下一页
                    more_links = _extract_pagination_links(r2.text, next_url)
                    for link in more_links:
                        if link not in visited:
                            next_links.append(link)
                except Exception:
                    break
        if images:
            return jsonify(images)

    # 非 wiki 站点也尝试 MediaWiki API 作为 fallback
    if not _is_wiki_site:
        wiki_images = _try_mediawiki_api(url)
        if wiki_images:
            return jsonify(wiki_images)

    # 最后用 Playwright 重试
    try:
        html = _playwright_fetch(url)
    except Exception as e:
        return jsonify({'error': f'无法访问该页面: {e}'}), 500

    # 检测 Playwright 是否也被 Cloudflare 拦截了
    if _is_cloudflare_page(html):
        return jsonify({'error': '该网站启用了 Cloudflare 防护，暂时无法抓取。请尝试其他网站，或直接上传图片。'}), 403

    images = _extract_images_from_html(html, url)
    return jsonify(images)


def _extract_json_images(obj, add_image):
    if isinstance(obj, str):
        if obj.startswith('http') and re.search(r'\.(jpg|jpeg|png|gif|webp|svg)', obj, re.I):
            add_image(obj, 'json_ld')
    elif isinstance(obj, list):
        for item in obj:
            _extract_json_images(item, add_image)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if re.search(r'image|photo|thumb|logo|banner|poster', k, re.I):
                if isinstance(v, str) and v.startswith('http'):
                    add_image(v, f'json_{k}')
                elif isinstance(v, dict) and v.get('url'):
                    add_image(v['url'], f'json_{k}')
            _extract_json_images(v, add_image)


# ── /proxy ────────────────────────────────────────────────────────
@app.route('/proxy', methods=['GET', 'OPTIONS'])
def proxy_image():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    image_url = request.args.get('url', '').strip()
    if not image_url:
        return jsonify({'error': 'url 参数缺失'}), 400
    try:
        r = do_request(image_url, stream=True, timeout=20)
        ct = r.headers.get('Content-Type', 'image/jpeg')
        if not r.content or len(r.content) < 100:
            return Response(b'', content_type='image/png', status=502)
        return Response(r.content, content_type=ct)
    except Exception as e:
        app.logger.warning('Proxy fetch failed for %s: %s', image_url, e)
        return Response(b'', content_type='image/png', status=502)


# ── /download-image （单张下载）──────────────────────────────────
@app.route('/download-image', methods=['POST', 'OPTIONS'])
def download_image():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    image_url = (data.get('url') or '').strip()
    alt_text = (data.get('alt') or 'image').strip()
    if not image_url:
        return jsonify({'error': 'url 参数缺失'}), 400
    try:
        r = do_request(image_url)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    ext = get_ext(image_url, r.headers.get('Content-Type', ''))
    filename = sanitize_filename(alt_text) + ext
    buf = io.BytesIO(r.content)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=filename,
                     mimetype=r.headers.get('Content-Type', 'image/jpeg'))


# ── /download-selected （批量下载为 ZIP）─────────────────────────
@app.route('/download-selected', methods=['POST', 'OPTIONS'])
def download_selected():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    images = data.get('images', [])
    if not images:
        return jsonify({'error': '未选择图片'}), 400

    def fetch_one(img):
        src = (img.get('src') or '').strip()
        alt = (img.get('alt') or 'image').strip()
        if not src:
            return None
        try:
            r = do_request(src, timeout=20)
        except Exception:
            return None
        ext = get_ext(src, r.headers.get('Content-Type', ''))
        filename = sanitize_filename(alt) + ext
        return (filename, r.content)

    zip_buf = io.BytesIO()
    seen_names: set[str] = set()

    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for result in ex.map(fetch_one, images):
                if result is None:
                    continue
                filename, content = result
                if filename in seen_names:
                    continue
                seen_names.add(filename)
                zf.writestr(filename, content)

    zip_buf.seek(0)
    return send_file(zip_buf, as_attachment=True,
                     download_name='images.zip',
                     mimetype='application/zip')


# ── API 代理：AI 图片生成 ───────────────────────────────────
@app.route('/api/generate', methods=['POST', 'OPTIONS'])
def api_generate():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    api_key = (data.get('apiKey') or '').strip()
    image_src = (data.get('imageSrc') or '').strip()
    platform = (data.get('platform') or 'apimart').strip().lower()
    if not api_key:
        return jsonify({'error': 'API Key 不能为空'}), 400
    if not image_src:
        return jsonify({'error': '图片源不能为空'}), 400

    # 如果是 URL，下载并转为 base64
    img_bytes = None
    if image_src.startswith('http'):
        try:
            r = do_request(image_src, timeout=30)
            img_bytes = r.content
        except Exception as e:
            return jsonify({'error': f'下载图片失败: {e}'}), 500
    else:
        # 已经是 base64 data URI
        try:
            header, b64_str = image_src.split(',', 1)
            img_bytes = base64.b64decode(b64_str)
        except Exception:
            return jsonify({'error': '无法解析图片数据'}), 400

    # ── 压缩 / 缩放图片，防止 base64 过大导致 API 失败 ──
    MAX_DIMENSION = 1024  # 最长边不超过 1024px
    MAX_BYTES = 1 * 1024 * 1024  # 压缩后不超过 1MB
    try:
        pil_img = PILImage.open(io.BytesIO(img_bytes))
        orig_w, orig_h = pil_img.size
        print(f'[generate] 原始图片尺寸: {orig_w}x{orig_h}, 原始大小: {len(img_bytes)} bytes', flush=True)

        # 如果有 alpha 通道，转为 RGB（JPEG 不支持透明）
        if pil_img.mode in ('RGBA', 'LA', 'P'):
            background = PILImage.new('RGB', pil_img.size, (255, 255, 255))
            if pil_img.mode == 'P':
                pil_img = pil_img.convert('RGBA')
            background.paste(pil_img, mask=pil_img.split()[-1] if pil_img.mode == 'RGBA' else None)
            pil_img = background

        if pil_img.mode != 'RGB':
            pil_img = pil_img.convert('RGB')

        # 按最长边缩放
        if max(orig_w, orig_h) > MAX_DIMENSION:
            ratio = MAX_DIMENSION / max(orig_w, orig_h)
            new_w = int(orig_w * ratio)
            new_h = int(orig_h * ratio)
            pil_img = pil_img.resize((new_w, new_h), PILImage.LANCZOS)
            print(f'[generate] 缩放至: {new_w}x{new_h}', flush=True)

        # 压缩为 JPEG，质量逐步降低直到低于 MAX_BYTES
        quality = 85
        while quality >= 30:
            buf = io.BytesIO()
            pil_img.save(buf, format='JPEG', quality=quality)
            compressed = buf.getvalue()
            if len(compressed) <= MAX_BYTES:
                break
            quality -= 10

        img_bytes = compressed
        print(f'[generate] 压缩后大小: {len(img_bytes)} bytes, quality={quality}', flush=True)
    except Exception as e:
        print(f'[generate] 图片压缩警告（使用原图）: {e}', flush=True)

    # 构建最终 data URI
    b64 = base64.b64encode(img_bytes).decode('utf-8')
    image_data = f"data:image/jpeg;base64,{b64}"

    # 固定输出比例为 2:3（1365×2048）
    size = '2:3'

    if platform == 'oneapi':
        # ── OneAPI 平台（gpt-4o 生图，同步返回）──
        try:
            chat_resp = requests.post(
                'https://oneapi.gptnb.ai/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                json={
                    'model': 'gpt-4o',
                    'messages': [
                        {
                            'role': 'user',
                            'content': [
                                {'type': 'text', 'text': COLORING_PROMPT},
                                {'type': 'image_url', 'image_url': {'url': image_data}}
                            ]
                        }
                    ],
                    'max_tokens': 4096,
                },
                timeout=180,
            )
            chat_body = chat_resp.json()
            print(f'[generate/oneapi] response: {str(chat_body)[:800]}', flush=True)
            if chat_body.get('error'):
                return jsonify({'error': chat_body['error'].get('message', 'OneAPI 生成失败')}), 400
            content = ''
            choices = chat_body.get('choices') or []
            if choices:
                content = (choices[0].get('message') or {}).get('content', '')
            # 从 markdown 内容提取图片 URL: ![xxx](url) 或直接 https://...
            import re as _re
            img_urls = _re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', content)
            if not img_urls:
                img_urls = _re.findall(r'(https?://[^\s\)"\']+\.(?:png|jpg|jpeg|webp|gif))', content)
            if not img_urls:
                # 检查是否有 base64 图片
                b64_matches = _re.findall(r'(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)', content)
                if b64_matches:
                    img_urls = b64_matches
            if img_urls:
                # 返回统一格式，task_id 用特殊前缀标记为已完成
                return jsonify({
                    'data': [{'task_id': f'oneapi_done__{img_urls[0]}'}]
                }), 200
            return jsonify({'error': f'OneAPI 返回内容中未找到图片: {content[:300]}'}), 400
        except Exception as e:
            return jsonify({'error': f'OneAPI 请求失败: {e}'}), 500

    elif platform == 'kie':
        # ── KIE.ai 平台（带 fallback: nano-banana-2 → nano-banana-pro）──
        try:
            # 上传图片到 KIE 文件存储
            upload_resp = requests.post(
                'https://kieai.redpandaai.co/api/file-stream-upload',
                headers={
                    'Authorization': f'Bearer {api_key}',
                },
                files={
                    'file': ('input.jpg', img_bytes, 'image/jpeg'),
                },
                data={
                    'uploadPath': 'coloring-page-inputs',
                },
                timeout=60,
            )
            upload_body = upload_resp.json()
            print(f'[generate/kie] upload response: {str(upload_body)[:500]}', flush=True)
            if not upload_body.get('success'):
                return jsonify({'error': f'KIE 文件上传失败: {upload_body.get("msg", "unknown")}'}), 400
            uploaded_url = (upload_body.get('data') or {}).get('downloadUrl') or (upload_body.get('data') or {}).get('fileUrl', '')
            if not uploaded_url:
                return jsonify({'error': 'KIE 文件上传成功但未返回 URL'}), 500

            # 尝试 nano-banana-2，失败则 fallback 到 nano-banana-pro
            kie_models = ['nano-banana-2', 'nano-banana-pro']
            last_err = ''
            for model_name in kie_models:
                resp = requests.post(
                    'https://api.kie.ai/api/v1/jobs/createTask',
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json'
                    },
                    json={
                        'model': model_name,
                        'input': {
                            'prompt': COLORING_PROMPT,
                            'aspect_ratio': size,
                            'resolution': '1K',
                            'output_format': 'jpg',
                            'image_input': [uploaded_url]
                        }
                    },
                    timeout=120
                )
                kie_body = resp.json()
                print(f'[generate/kie/{model_name}] response: {str(kie_body)[:800]}', flush=True)
                if kie_body.get('code') == 200:
                    task_id = (kie_body.get('data') or {}).get('taskId')
                    if task_id:
                        return jsonify({'data': [{'task_id': task_id}]}), 200
                last_err = kie_body.get('msg', f'{model_name} 任务创建失败')
                print(f'[generate/kie] {model_name} 失败: {last_err}, 尝试下一个模型', flush=True)
            return jsonify({'error': f'KIE 所有模型均失败: {last_err}'}), 400
        except Exception as e:
            return jsonify({'error': f'KIE API 请求失败: {e}'}), 500
    else:
        # ── Apimart 平台（带 fallback: gemini-3.1-flash → gemini-3-pro）──
        apimart_models = ['gemini-3.1-flash-image-preview', 'gemini-3-pro-image-preview']
        last_err = ''
        for model_name in apimart_models:
            try:
                resp = requests.post(
                    'https://api.apimart.ai/v1/images/generations',
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json'
                    },
                    json={
                        'model': model_name,
                        'prompt': COLORING_PROMPT,
                        'image_urls': [image_data],
                        'size': size,
                        'resolution': '1K',
                        'n': 1
                    },
                    timeout=120
                )
                body = resp.json()
                print(f'[generate/apimart/{model_name}] status={resp.status_code}, resp: {str(body)[:500]}', flush=True)
                if resp.status_code == 200 and not body.get('error'):
                    return jsonify(body), 200
                last_err = body.get('error', {}).get('message', '') if isinstance(body.get('error'), dict) else str(body.get('error', model_name + ' 失败'))
                print(f'[generate/apimart] {model_name} 失败: {last_err}, 尝试下一个模型', flush=True)
            except Exception as e:
                last_err = str(e)
                print(f'[generate/apimart] {model_name} 异常: {e}, 尝试下一个模型', flush=True)
        return jsonify({'error': f'Apimart 所有模型均失败: {last_err}'}), 500


@app.route('/api/task/<task_id>', methods=['GET', 'OPTIONS'])
def api_task(task_id):
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    api_key = request.args.get('apiKey', '').strip()
    platform = request.args.get('platform', 'apimart').strip().lower()
    if not api_key:
        return jsonify({'error': 'API Key 不能为空'}), 400

    # OneAPI 同步模式不需要轮询
    if platform == 'oneapi':
        return jsonify({'status': 'completed', 'data': {'images': []}}), 200

    if platform == 'kie':
        # ── KIE.ai 轮询 ──
        try:
            resp = requests.get(
                f'https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=30
            )
            kie_body = resp.json()
            import json as _json
            print(f'[task/{task_id}] kie response ({resp.status_code}): {_json.dumps(kie_body, ensure_ascii=False)[:800]}', flush=True)
            kie_data = kie_body.get('data') or {}
            state = (kie_data.get('state') or '').lower()
            # 映射 KIE 状态到前端通用状态
            status_map = {'success': 'completed', 'fail': 'failed'}
            normalized_status = status_map.get(state, 'processing')
            result = {'status': normalized_status, 'data': {}}
            if normalized_status == 'completed':
                result_json_str = kie_data.get('resultJson') or '{}'
                try:
                    result_json = json.loads(result_json_str) if isinstance(result_json_str, str) else result_json_str
                    urls = result_json.get('resultUrls', [])
                    result['data']['images'] = [{'url': u} for u in urls]
                except Exception:
                    result['data']['images'] = []
            elif normalized_status == 'failed':
                result['error'] = {'message': kie_data.get('failMsg') or kie_data.get('errorMsg') or '任务失败'}
            return jsonify(result), 200
        except Exception as e:
            return jsonify({'error': f'查询KIE任务失败: {e}'}), 500
    else:
        # ── Apimart 轮询 ──
        try:
            resp = requests.get(
                f'https://api.apimart.ai/v1/tasks/{task_id}?language=zh',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=30
            )
            body = resp.json()
            import json as _json
            print(f'[task/{task_id}] apimart response ({resp.status_code}): {_json.dumps(body, ensure_ascii=False)[:800]}', flush=True)
            return jsonify(body), resp.status_code
        except Exception as e:
            return jsonify({'error': f'查询任务失败: {e}'}), 500


# ── API：AI 智能重命名 ───────────────────────────────────────
@app.route('/api/rename', methods=['POST', 'OPTIONS'])
def api_rename():
    """接收主题词和图片列表，用后端提示词模板调用 Chat Completions。"""
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    api_key = (data.get('apiKey') or '').strip()
    theme = (data.get('theme') or '').strip()
    images = data.get('images', [])  # [{ url: 'data:...' }]
    if not api_key:
        return jsonify({'error': 'API Key 不能为空'}), 400
    if not theme:
        return jsonify({'error': '主题词不能为空'}), 400
    if not images:
        return jsonify({'error': '图片列表不能为空'}), 400

    # 用后端模板生成提示词
    rename_prompt = RENAME_PROMPT_TEMPLATE.format(count=len(images), theme=theme)

    # 构建 vision 消息
    image_contents = [{'type': 'image_url', 'image_url': {'url': img['url']}} for img in images]
    messages = [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': rename_prompt},
            *image_contents
        ]
    }]

    try:
        resp = requests.post(
            'https://api.apimart.ai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'gpt-5',
                'messages': messages,
                'temperature': 0.3,
                'stream': False
            },
            timeout=180
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({'error': f'Chat API 请求失败: {e}'}), 500


# ── 打包重命名后的填色页为 ZIP ──────────────────────────────
@app.route('/api/download-renamed', methods=['POST', 'OPTIONS'])
def download_renamed():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    items = data.get('items', [])
    if not items:
        return jsonify({'error': '没有可下载的图片'}), 400

    zip_buf = io.BytesIO()
    seen_names: set[str] = set()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            name = (item.get('name') or 'image').strip()
            image_data = (item.get('imageData') or '').strip()
            if not image_data:
                continue
            try:
                if image_data.startswith('data:'):
                    _, b64_str = image_data.split(',', 1)
                    img_bytes = base64.b64decode(b64_str)
                else:
                    img_bytes = base64.b64decode(image_data)
            except Exception:
                continue
            # 确保文件名唯一
            fname = name + '.png'
            if fname in seen_names:
                i = 2
                while f'{name}_{i}.png' in seen_names:
                    i += 1
                fname = f'{name}_{i}.png'
            seen_names.add(fname)
            zf.writestr(fname, img_bytes)

    zip_buf.seek(0)
    return send_file(zip_buf, as_attachment=True,
                     download_name='coloring_pages_renamed.zip',
                     mimetype='application/zip')


# ── 德语词表对照（来自 xlsx 对照表） ────────────────────────────
EN_DE_DICT = {
    "Sports": "Sport",
    "Cars": "Autos",
    "Anime": "Anime",
    "Landscapes": "Landschaften",
    "Seasons": "Jahreszeiten",
    "Animals": "Tiere",
    "Insects": "Insekten",
    "Holidays": "Feiertage",
    "Months": "Monate",
    "Religious Holidays": "Religiöse Feiertage",
    "International Holidays": "Internationale Feiertage",
    "Common Holidays": "Allgemeine Feiertage",
    "Transportation": "Verkehrsmittel",
    "Flowers and Plants": "Blumen und Pflanzen",
    "Airplanes": "Flugzeuge",
    "Ships": "Schiffe",
    "Spaceships": "Raumschiffe",
    "Food": "Essen",
    "Fruits": "Früchte",
    "Vegetables": "Gemüse",
    "Desserts": "Desserts",
    "Fantasy": "Fantasie",
    "Internet Trends": "Internettrends",
    "Sanrio": "Sanrio",
    "Lifestyle": "Lebensstil",
    "Historical Figures": "Historische Persönlichkeiten",
    "Superheroes": "Superhelden",
    "Animated Movies": "Animationsfilme",
    "Japanese Anime": "Japanische Anime",
    "American Anime": "Amerikanische Zeichentrickserien",
    "Chinese Anime": "Chinesische Anime",
    "Games": "Spiele",
    "Video Games": "Videospiele",
    "Mobile Games": "Mobile Games",
    "Characters": "Figuren",
    "Football Stars": "Fußballstars",
    "Celebrity": "Promis",
    "Nature": "Natur",
    "Occupations": "Berufe",
    "Disney Princesses": "Disney Prinzessinnen",
    "Themes": "Themen",
    "Architecture": "Architektur",
    "Religion": "Religion",
    "Lego": "Lego",
    "Marvel": "Marvel",
    "DC": "DC",
    "Pixar": "Pixar",
    "DreamWorks": "DreamWorks",
    "Disney": "Disney",
    "Studio Ghibli": "Studio Ghibli",
    "Horror": "Horror",
    "Clothing": "Kleidung",
    "Toys": "Spielzeug",
}

# 构建小写查找表
_en_de_lower = {k.lower(): v for k, v in EN_DE_DICT.items()}


@app.route('/api/dict', methods=['GET', 'OPTIONS'])
def api_dict():
    """返回完整词表"""
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    return jsonify(EN_DE_DICT)


@app.route('/api/translate', methods=['POST', 'OPTIONS'])
def api_translate():
    """输入多行文本，每行逗号分隔的英文标签 → 查词表替换为德文，保持格式不变"""
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    data = request.get_json(silent=True) or {}
    lines = data.get('lines', [])
    if not lines or not isinstance(lines, list):
        return jsonify({'error': '请提供 lines 数组'}), 400

    result_lines = []
    total = 0
    matched = 0
    unmatched_set = set()
    for line in lines:
        tags = [t.strip() for t in str(line).split(',') if t.strip()]
        de_tags = []
        for tag in tags:
            total += 1
            lower = tag.lower()
            if lower in _en_de_lower:
                de_tags.append(_en_de_lower[lower])
                matched += 1
            else:
                de_tags.append(tag)  # 未匹配保留原文
                unmatched_set.add(tag)
        result_lines.append(','.join(de_tags))

    return jsonify({
        'result_lines': result_lines,
        'total': total,
        'matched': matched,
        'unmatched': sorted(unmatched_set),
    })


# ══════════════════════════════════════════════════════════════
# SEO 文案生成 / 重写  —  直接调用 apimart.ai API
# ══════════════════════════════════════════════════════════════
APIMART_URL = 'https://api.apimart.ai/v1/chat/completions'
STYLE_CRAWL_MAX_URLS = 3
STYLE_CRAWL_TIMEOUT = 4.5
STYLE_CRAWL_MAX_PAGE = 120_000
STYLE_CRAWL_MAX_SAMPLE = 900

# ── Section 提示词定义 ──
SECTION_PROMPTS_DEF = {
    'seo_metadata': (
        '**SEO Metadata (网页元数据)**\n'
        '- **Title**: 长度控制在 40-55 字符（含标点符号&空格）。必须包含完整的核心关键词&1-2个有效词根，必须出现品牌名，且具有高点击率的吸引力。\n'
        '  写作时合理选择词根（Free/Best/Online等），不可照搬示例，需合理更换词根/语序。\n'
        '  写作时需要将次要关键词**拆分**埋入，进行关键词的广泛匹配，**不可强行堆叠**，'
        '示例：核心关键词：AI Coloring Page Generator，词根：free，次要关键词：printable coloring pages，'
        '那么结果就应当是：Free AI Coloring Page Generator | Create Printable Coloring Pages Online\n'
        '- **Description**: 长度控制在 140-150 字符。概括页面核心价值，包含关键词，并有行动号召，第一句需包含核心关键词。'
    ),
    'hero': (
        '**Hero / 页面首屏**\n'
        '- Tag: ≤6词，可选含核心关键词，不可全大写\n'
        '- H1 标题：包含核心关键词1次，不包含品牌名。不要出现标点符号，连词符，冒号。\n'
        '- H2 副标题：价值主张\n'
        '- 一个有说服力的Description，要使用核心关键词和次要关键词进行密度提升，清晰定义产品是什么，解决什么核心问题。\n'
        '- CTA按钮文本'
    ),
    'whatIs': (
        '**What Is / 页面首屏介绍 (IP7 模块)**\n'
        '用来快速告诉用户这个产品是什么、能做什么、适合谁，并推动点击按钮。\n\n'
        '需要生成的字段（对应 JSON key）：\n'
        '- h2 (TitleName)：主标题，6-8个词，清晰表达核心关键词的核心价值。\n'
        '- description (DescriptionName)：介绍文案，2句话，40-60词。第一句解释产品做什么，第二句说明适合哪些用户或使用场景。'
        '要使用核心关键词和次要关键词进行密度提升。\n'
        '- imageAlt (ImgAltName)：图片alt文本，3-8个词，根据描述来生成，不要堆关键词。\n'
        '- cta (BtnName)：CTA按钮，2-4个词，简洁有行动感。\n\n'
        '风格专业、自然、偏转化，不要夸张，不要空话。'
    ),
    'showcase': (
        '**Showcase (IP6 模块)**\n'
        '这是 Showcase 模块，用来展示与核心词相关的填色页主题。\n'
        '主要是列举填色页主题和文案。举例：核心词为 adult coloring pages printable，'
        '你就要想关于这个核心词成年人会生成什么主题的填色页，这个主题的填色页用 description 怎么描述。\n\n'
        '需要生成的字段：\n'
        '- tag：短标签，2-5个词。\n'
        '- h2：showcase 相关的引导性标题，自然融入1次核心关键词。\n'
        '- description：1-2句话，30-40词，说明填色页有以下风格的主题等待用户探索之类的引导文案。'
        '要使用核心关键词和次要关键词进行密度提升。\n'
        '- items 数组：必须恰好 **{{count}}** 个元素，每个元素是一张填色页主题卡片。\n'
        '  每张卡片必须包含以下 3 个字段，缺一不可：\n'
        '  - title：不同的填色页主题，2-6个词\n'
        '  - description：1-2句话，20-30词，写出每个主题的相关文案，描述中要体现核心关键词或次要关键词\n'
        '  - imageAlt：图片alt文本，3-8个词，根据 description 文案来生成，不要堆关键词\n\n'
        '  ❗ 每张卡片的主题必须完全不同，不能只换说法！\n'
        '  ❗ items 数组必须有 {{count}} 个元素，每个都包含 title + description + imageAlt！\n\n'
        '- cta：2-4个词。'
    ),
    'whyChooseUs': (
        '**Why Choose Us (IP1 模块)**\n'
        '概括产品优势，通过 {{count}} 张优势卡片展示核心卖点。\n\n'
        '需要生成的字段：\n'
        '- tag (TagName)：短标签，2-5个词。\n'
        '- h2 (TitleName)：模块标题，4-8个词。\n'
        '- description (DescriptionName)：模块总描述，1-2句话，20-25词，少于20词视为不合格。禁止采用whether..句式。'
        '要使用核心关键词和次要关键词进行密度提升。\n'
        '- items 数组（{{count}}个），每项包含：\n'
        '  - title (ItemTitleName)：2-5个词\n'
        '  - description (ItemDescriptionName)：2句话，30-40词\n'
        '  - 每张卡片讲不同卖点，避免重复\n'
        '- cta (BtnName)：2-4个词，适合作为底部按钮。\n\n'
        '语言要像真实SaaS官网，不要模板腔。'
    ),
    'features': (
        '**Features (IP9 模块)**\n'
        '**重要：feature 部分的文案需要参考用户输入的"页面功能描述"，'
        '将功能描述中的内容提炼为具体功能点，不要泛泛而谈。**\n\n'
        'items 数组必须恰好 **{{count}}** 个元素，每个元素是一个独立功能卡片。\n'
        '每个 feature 必须包含以下全部 6 个字段，缺一不可：\n'
        '- tag：如 Feature 1 / Smart Feature / Creator Tool 一类短标签，2-4个词。\n'
        '- title：功能标题，3-7个词。\n'
        '- description：第一段功能说明，1句话，18-35词，解释功能本身。描述中要体现核心关键词或次要关键词。\n'
        '- description2：第二段补充说明，1句话，18-35词，解释结果、优势或适用场景。描述中要体现核心关键词或次要关键词。\n'
        '- imageAlt：图片alt文本，3-8个词，根据 description 文案来生成，不要堆关键词。\n'
        '- cta：2-4个词，按钮文案。\n\n'
        '❗ 每个 feature 都要是不同功能点，不能重复！\n'
        '❗ items 数组必须有 {{count}} 个元素，每个元素都包含 tag + title + description + description2 + imageAlt + cta！\n\n'
        '文案要偏产品功能，不要写成品牌宣传口号。'
    ),
    'howItWork': (
        '**How It Works**\n'
        '- Tag: ≤6词，可适当包含核心关键词，不可全大写\n'
        '- H2 标题：含核心关键词上下文，不得出现品牌名。不要出现标点符号，连词符，冒号。\n'
        '- Description：20-25词，少于20词视为不合格。禁止采用whether..句式。要使用核心关键词和次要关键词进行密度提升。\n'
        '- 3个步骤，每步包含 title（不要带序号） + description（20-25词）\n'
        '- CTA按钮文本'
    ),
    'faq': (
        '**FAQ (IP2 模块)**\n'
        '{{count}}组常见问答，适用于填色页产品页面。\n\n'
        '**FAQ的核心SEO意义：合理的埋入次要关键词（叠加词根），问题的回答尽量不要出现核心关键词，避免密度过高。**\n'
        '例如：问题可以包含次要关键词，如 "What Are the Best Printable Coloring Pages for Adults?"\n\n'
        '需要生成的字段：\n'
        '- tag (TagName)：FAQ 或类似短标签。\n'
        '- h2 (TitleName)：FAQ 模块标题。\n'
        '- description (DescriptionName)：一句简短说明，引导用户查看常见问题。\n'
        '- items 数组（{{count}}个），每项包含：\n'
        '  - question (ItemQuestionName)：自然口语化，像真实用户会问的问题，问题中要自然融入次要关键词，问题长度控制在10-15词\n'
        '  - answer (ItemAnswerName)：2-3句话，40-60词，回答清楚，不能空泛。'
        '回答中要自然融入次要关键词（不同问题使用不同的次要关键词），但尽量避免核心关键词以防密度过高\n'
        '  - 回答要真实可信，不要过度承诺，不要像广告词\n'
        '  - 问题要涵盖不同方面（功能、用法、适用人群等），确保覆盖尽可能多的次要关键词'
    ),
    'cta': (
        '**CTA (IP8 模块)**\n'
        '页面底部 CTA，再次推动用户开始使用产品。\n\n'
        '需要生成的字段：\n'
        '- headline (TitleName)：6-12个词，清楚表达行动号召。需要出现核心关键词。不要出现标点符号，连词符，冒号。\n'
        '- description (DescriptionName)：1-2句话，15-25词。禁止采用whether..句式。'
        '要使用核心关键词和次要关键词进行密度提升。\n'
        '- cta (BtnName)：2-4个词，简短有力。\n\n'
        '整体风格像SaaS官网底部CTA，不要太长，不要空洞。'
    ),
}


def _generate_json_schema(sections, include_metadata):
    """生成 JSON 输出格式示例，每个 item 用序号标注以确保 AI 生成完整数量"""
    schema = {}
    if include_metadata:
        schema['seo_metadata'] = {'title': 'string', 'description': 'string'}
    for sec in sections:
        sid = sec['id']
        count = (sec.get('options') or {}).get('count', 3)
        alt = sec.get('alt', False)
        if sid == 'whatIs':
            schema[sid] = {'h2': 'string', 'description': 'string', 'imageAlt': 'string', 'cta': 'string'}
        elif sid == 'hero':
            s = {'tag': 'string', 'h2': 'string', 'description': 'string', 'cta': 'string'}
            if alt:
                s['imageAlt'] = 'string'
            schema[sid] = s
        elif sid == 'showcase':
            # IP6: 每个 item 用序号标记，确保 AI 生成完整的 count 个
            items = []
            for i in range(1, count + 1):
                items.append({'title': f'Card{i} title', 'description': f'Card{i} description', 'imageAlt': f'Card{i} imageAlt'})
            schema[sid] = {'tag': 'string', 'h2': 'string', 'description': 'string', 'items': items, 'cta': 'string'}
        elif sid in ('howItWork', 'howToUse'):
            steps = []
            for i in range(1, 4):
                step = {'title': f'Step{i} title', 'description': f'Step{i} description'}
                if alt:
                    step['imageAlt'] = f'Step{i} imageAlt'
                steps.append(step)
            schema[sid] = {'tag': 'string', 'h2': 'string', 'description': 'string', 'steps': steps, 'cta': 'string'}
        elif sid == 'features':
            # IP9: 每个 feature 用序号标记
            items = []
            for i in range(1, count + 1):
                items.append({
                    'tag': f'Feature{i} tag', 'title': f'Feature{i} title',
                    'description': f'Feature{i} description', 'description2': f'Feature{i} description2',
                    'imageAlt': f'Feature{i} imageAlt', 'cta': f'Feature{i} cta'
                })
            schema[sid] = {'items': items}
        elif sid in ('whyChooseUs', 'whoCanBenefit'):
            items = []
            for i in range(1, count + 1):
                item = {'title': f'Item{i} title', 'description': f'Item{i} description'}
                if alt:
                    item['imageAlt'] = f'Item{i} imageAlt'
                items.append(item)
            schema[sid] = {'tag': 'string', 'h2': 'string', 'description': 'string', 'items': items, 'cta': 'string'}
        elif sid == 'faq':
            items = []
            for i in range(1, count + 1):
                items.append({'question': f'FAQ{i} question', 'answer': f'FAQ{i} answer'})
            schema[sid] = {'tag': 'string', 'h2': 'string', 'description': 'string', 'items': items}
        elif sid == 'cta':
            # IP8: headline, description, cta (no tag)
            schema[sid] = {'headline': 'string', 'description': 'string', 'cta': 'string'}
    return json.dumps(schema, indent=2, ensure_ascii=False)


def _strip_code_fence(text):
    """移除 markdown 代码块标记"""
    t = (text or '').strip()
    t = re.sub(r'^```(?:markdown|md|json)?', '', t)
    t = re.sub(r'```$', '', t)
    return t.strip()


def _html_to_plain(html):
    """简单 HTML → 纯文本"""
    text = re.sub(r'<script[\s\S]*?</script>', ' ', html or '', flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;|&#160;', ' ', text, flags=re.I)
    text = re.sub(r'&amp;', '&', text, flags=re.I)
    text = re.sub(r'&lt;', '<', text, flags=re.I)
    text = re.sub(r'&gt;', '>', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _trim_to_sentence(text, max_chars=900):
    clean = (text or '').strip()
    if not clean:
        return ''
    if len(clean) <= max_chars:
        return clean
    sliced = clean[:max_chars]
    last_stop = max(sliced.rfind('. '), sliced.rfind('! '), sliced.rfind('? '))
    if last_stop > 200:
        return sliced[:last_stop + 1].strip()
    return sliced.strip() + '...'


def _fetch_style_sample(url):
    """抓取 URL 页面，提取文本样本用于风格模仿"""
    try:
        resp = requests.get(url, timeout=STYLE_CRAWL_TIMEOUT, headers={
            'Accept': 'text/html, text/plain;q=0.9,*/*;q=0.8',
            'User-Agent': 'Mozilla/5.0 (compatible; SeoWriterStyleBot/1.0)'
        })
        if not resp.ok:
            return None
        ct = resp.headers.get('content-type', '')
        if 'text/html' not in ct and 'text/plain' not in ct:
            return None
        raw = resp.text[:STYLE_CRAWL_MAX_PAGE]
        # 提取主体内容
        for tag in ('main', 'article', 'body'):
            m = re.search(rf'<{tag}[\s\S]*?</{tag}>', raw, re.I)
            if m:
                raw = m.group(0)
                break
        plain = _html_to_plain(raw)
        sample = _trim_to_sentence(plain, STYLE_CRAWL_MAX_SAMPLE)
        return sample if sample and len(sample) >= 120 else None
    except Exception:
        return None


def _build_style_mimic_block(payload):
    """构建风格模仿提示词块"""
    raw_urls = payload.get('styleReferenceUrls') or []
    if isinstance(raw_urls, str):
        raw_urls = [u.strip() for u in re.split(r'[,\n，]', raw_urls) if u.strip()]
    urls = [u for u in raw_urls[:5] if re.match(r'^https?://', u, re.I)]

    raw_samples = payload.get('styleReferenceSamples') or []
    if isinstance(raw_samples, str):
        raw_samples = [s.strip() for s in re.split(r'[,\n，]', raw_samples) if s.strip()]
    samples = [(s[:360] + '...') if len(s) > 360 else s for s in raw_samples[:5]]

    notes = (payload.get('styleMimicNotes') or '').strip()
    mode_raw = (payload.get('styleMimicMode') or 'balanced').lower()

    mode_map = {
        'light': '轻度模仿（保留本站原风格为主，仅借鉴节奏与措辞温度）',
        'balanced': '平衡模仿（借鉴句法、语气与叙事组织，但保持品牌差异）',
        'strong': '强化模仿（在不抄袭前提下显著贴近目标风格指纹）',
    }
    mode_text = mode_map.get(mode_raw, mode_map['balanced'])

    if not urls and not samples and not notes:
        return ''

    url_block = ('- 参考网址（仅学习写作风格，不复制事实内容）：\n' + '\n'.join(f'  - {u}' for u in urls)) if urls else '- 参考网址：未提供'
    sample_block = ('- 参考文本片段（用于提取风格指纹）：\n' + '\n'.join(f'  - 样本{i+1}: {s}' for i, s in enumerate(samples))) if samples else '- 参考文本片段：未提供'
    notes_block = f'- 额外风格备注：{notes}' if notes else '- 额外风格备注：无'

    return (
        '【风格模仿任务（高优先级：先分析再写作）】\n'
        f'{url_block}\n{sample_block}\n{notes_block}\n'
        f'- 模仿强度：{mode_text}\n'
        '- 执行流程：\n'
        '  1. 先提取"风格指纹"：句长分布、常用动词、开场方式、证据表达、CTA力度、禁用套话。\n'
        '  2. 输出时只迁移"表达方式"，不可复写原文事实、案例、数据或专有句子。\n'
        '  3. 每个 section 使用不同句法节奏，避免整页同一模板句。\n'
        '  4. 至少在 3 个 section 出现可感知的人类写作痕迹：具体场景、真实动作、结果反馈。\n'
        '  5. 严禁抄袭：连续 8 个及以上英文单词不得与参考文本完全一致。'
    )


def _enrich_style_samples(payload):
    """抓取参考 URL 的文本样本，注入 payload"""
    if payload.get('styleCrawlEnabled') is False:
        return payload
    raw_urls = payload.get('styleReferenceUrls') or []
    if isinstance(raw_urls, str):
        raw_urls = [u.strip() for u in re.split(r'[,\n，]', raw_urls) if u.strip()]
    urls = [u for u in raw_urls[:STYLE_CRAWL_MAX_URLS] if re.match(r'^https?://', u, re.I)]
    if not urls:
        return payload

    existing = payload.get('styleReferenceSamples') or []
    if isinstance(existing, str):
        existing = [existing]
    fetched = []
    for url in urls:
        sample = _fetch_style_sample(url)
        if sample:
            fetched.append(sample)
    if not fetched:
        return payload
    merged = (list(existing) + fetched)[:5]
    payload = dict(payload)
    payload['styleReferenceSamples'] = merged
    return payload


def _build_generation_prompt(payload):
    """构建 SEO 文案生成提示词"""
    core_kw = payload.get('coreKeyword', '')
    brand = payload.get('brandName', 'iPage')
    sections = payload.get('sections', [])
    density = payload.get('densityTarget', 2.0)
    secondary_kw = payload.get('secondaryKeywords', '')
    custom_prompt = payload.get('customPrompt', '')
    include_metadata = payload.get('includeMetadata', False)
    include_brand = payload.get('includeBrandInTitle', True)
    style_mimic_block = _build_style_mimic_block(payload)

    density_sec = payload.get('densityTargetSecondary', 3.0)

    # 构建各 section 详细指令
    details = []
    if include_metadata:
        meta_prompt = SECTION_PROMPTS_DEF['seo_metadata']
        if not include_brand:
            meta_prompt = meta_prompt.replace('必须出现品牌名', 'Title 不要包含品牌名')
        details.append(meta_prompt)

    for sec in sections:
        sid = sec['id']
        content_reqs = SECTION_PROMPTS_DEF.get(sid, f'请撰写符合该板块 ({sec.get("label", sid)}) 功能的营销文案。')
        count = (sec.get('options') or {}).get('count', 3)
        content_reqs = content_reqs.replace('{{count}}', str(count))
        # 对于 showcase/features/whatIs，imageAlt 已在 SECTION_PROMPTS_DEF 和 schema 中内置，
        # 其余 section 若 alt=true 则追加 alt 指令
        if sec.get('alt') and sid not in ('showcase', 'features', 'whatIs'):
            content_reqs += f'\n- **Image Alt Text**: 为该板块生成 **{count}** 条图片描述 (Alt Text)。每条描述需为6-8个单词并包含核心关键词。'
        # Features 专门注入页面功能描述（直接把用户输入的功能描述原文嵌入）
        if sid == 'features' and custom_prompt:
            content_reqs += (
                f'\n\n【⚠️ 页面功能描述 — features 的每个功能点必须严格依据以下内容撰写】\n'
                f'用户提供的功能描述原文如下：\n'
                f'"""\n{custom_prompt}\n"""\n'
                f'要求：将以上功能描述拆解为 {count} 个具体功能点，每个 feature 的 title 和 description 必须对应上述描述中的某个功能。'
                f'不要自己编造功能点，不要泛泛而谈，必须基于以上原文提炼。'
            )
        details.append(content_reqs)

    sections_detail = '\n\n'.join(details)
    json_schema = _generate_json_schema(sections, include_metadata)

    # 构建次要关键词列表（逐个列出以强调每个都必须出现）
    sec_kw_list = [k.strip() for k in secondary_kw.replace('\n', ',').split(',') if k.strip()] if secondary_kw else []
    sec_kw_enumerated = ''
    if sec_kw_list:
        sec_kw_enumerated = '\n'.join(f'   {i+1}. "{kw}" — 必须至少出现1次' for i, kw in enumerate(sec_kw_list))

    prompt = f"""你是一个专业的 SaaS 官网 SEO 文案写手，同时也是一个理解 JSON 页面模块结构的内容编辑。
你也是一个SEO专家，擅长根据关键词搜索资讯并写出不同版本的清晰易懂，能吸引用户点击停留，简单精炼高质量且符合SEO原则的英语文案。

新任务：为填色页网站 "{brand}" 写一篇关键词为 "{core_kw}" 的功能专题页文案。
请根据SEO原则分析这个关键词的使用场景、用户群体和需求痛点，并以此展开文案撰写。
你的任务不是改 JSON 结构，而是只为指定字段生成英文文案内容，便于后续直接回填到 JSON 对应字段中。
语言风格：精炼、简单、直接、用户最常用、最好理解的英语表达。

【写作目标】
1. 风格要自然、专业、清晰，避免明显 AI 腔。
2. 文案要适合 SaaS 官网落地页，兼顾 SEO 与转化。
3. 不要写得过空，不要只是泛泛而谈，要突出真实使用场景、功能价值、受众收益。
4. 所有输出都必须和字段用途匹配，例如：
   - h2 / TitleName → 写标题
   - description / DescriptionName → 写描述
   - cta / BtnName → 写按钮文案
   - question / ItemQuestionName → 写 FAQ 问题
   - answer / ItemAnswerName → 写 FAQ 回答
5. 默认输出英文。
6. 如果一个模块里有多个卡片/feature/FAQ，请分别输出并保持在 items 数组中。
7. 按 SaaS 落地页习惯：标题简洁有力、描述信息完整、按钮短促直接、FAQ 回答自然可信非夸张。
8. 写出的文案要参考爬虫抓取的竞品网址文案（如已提供）。
9. feature 部分的文案需要参考用户输入的「页面功能描述」文案。

{style_mimic_block + chr(10) if style_mimic_block else ''}【反 AI 腔写作规则（高优先级）】
- 避免模板开头：In today's world / Whether you are / In the realm of / Imagine a world
- 禁用空洞词汇：Stunning, Empower, Imagine, Revolutionize, Unlock, Transform, Elevate, Unleash
- 同一页面内不要重复同一动词开头句式
- 首句即价值主张，避免空开场

【SEO 关键词密度要求 — 必须严格遵守，这是最重要的部分】
关键词密度计算公式：密度 = (关键词出现次数 ÷ 文章总词数) × 100%

A. 核心关键词 "{core_kw}"：
   - 密度目标: {density}%（允许范围: {density - 0.3}% ~ {density + 0.3}%）
   - 核心词必须出现于:
     1. SEO Title
     2. Meta Description
     3. H1
     4. 首段
     5. 页面重要模块（Features、FAQ、示例内容）
   - 不要堆砌，自然融入句子

B. 次要关键词: {secondary_kw or '无'}
   - **每个次要关键词必须至少出现 1 次，至多 3 次**，不得超出次数，根据语境灵活使用
   - 次要关键词总出现次数的密度目标: {density_sec}%（允许范围: {density_sec - 0.5}% ~ {density_sec + 0.5}%）
   - 在保证文案自然流畅且地道的前提下，挑选次要关键词中有搜索量的词表达，提升文案搜索引擎权重的同时不破坏阅读体验
   - 自然融入 description、answer、title 等字段，不要集中在一处
{"   - 次要关键词逐个检查清单（每个都必须至少出现1次）：" + chr(10) + sec_kw_enumerated + chr(10) if sec_kw_enumerated else ''}   - 次要关键词分配策略（按优先级）：
     1. FAQ 问题和回答是埋入次要关键词的最佳位置（每个FAQ问答中使用1-2个不同的次要关键词）
     2. Features 的 description 中自然融入次要关键词
     3. Showcase 的 item description 中融入次要关键词
     4. SEO Title 中将次要关键词拆分埋入
     5. 各 section 的 Description 字段也是融入次要关键词的好位置

C. 词根策略：
   - 挑选 1-3 个常见词根（如 free, best, online, printable, download），确保至少出现一次
   - 应用方式：Title & Description 中适量出现；页面模块中自然出现（Features / CTA / FAQ 等）；不可堆砌

【SEO 格式要求】
- SEO Title要求：xxx(核心关键词要出现) | xxx(次要关键词拆散重组)
- 主标题文本要求：不要出现标点符号，连词符，冒号。不要使用brandname:这种格式，要是一句完整通畅的短句，同时不得出现brandname
- Description文本要求：不同section的Description不能套用相同的句式，页面文案句式需要顺畅且多变
- 除 SEO Metadata 外，所有 section 的 description 长度统一控制在20-25词；少于20词视为不合格，必须补充到范围内
- CTA按钮文本要求：不要使用[]框选,只能有文本不能有符号，不可过于冗长，返回正常的文本，不用强制出现核心关键词，**必须是1-2个词**，比如：Try Now, Explore

- 页面功能补充: {custom_prompt or '无额外要求'}

【内容结构要求 - 各模块详细说明】
{sections_detail}

【重要 - 输出格式】
**必须输出纯 JSON 格式！**

按以下 JSON 结构输出：

{json_schema}

注意事项：
1. 直接输出 JSON，不要使用 markdown 代码块
2. 所有字段都必须是字符串类型
3. 数组字段(items, steps)必须包含指定数量的元素
4. 严格遵守以上所有 SEO 要求和内容要求

请现在开始生成 JSON："""
    return prompt


def _build_rewrite_prompt(payload):
    """构建 SEO 文案重写提示词"""
    core_kw = payload.get('coreKeyword', '')
    brand = payload.get('brandName', 'iPage')
    section_id = payload.get('rewriteSectionId', '')
    section_label = payload.get('rewriteSectionLabel', section_id)
    rewrite_prompt = payload.get('customRewritePrompt', '')
    sections = payload.get('sections', [])
    style_mimic_block = _build_style_mimic_block(payload)

    section_def = SECTION_PROMPTS_DEF.get(section_id, f'请撰写符合 "{section_label}" 功能的专业营销文案。')
    target_sec = next((s for s in sections if s.get('id') == section_id), None)
    if target_sec:
        count = (target_sec.get('options') or {}).get('count', 3)
        section_def = section_def.replace('{{count}}', str(count))
    section_def = section_def.replace('{{count}}', '若干')

    return f"""你是一个SEO专家，擅长根据关键词搜索资讯并写出不同版本的清晰易懂，能吸引用户点击停留，简单精炼高质量且符合SEO原则的英语文案。
任务：重新撰写网站 "{brand}" 的 "{section_label}" (ID: {section_id}) 板块文案。

【核心背景】
- 核心关键词: "{core_kw}"
- 语言: 英文
- 风格: 精炼、简单、直接、用户最常用、最好理解的英语表达。

{style_mimic_block + chr(10) if style_mimic_block else ''}【结构要求】
{section_def}

【修改指令 (用户需求)】
"{rewrite_prompt or '无特殊要求'}"

【输出规则】
请直接输出重写后的 **英文 Markdown 文本**。
不需要 JSON 格式，不需要分隔符，不需要任何解释性文字。直接开始写内容。
"""


# ── 服务端关键词密度校验 ──────────────────────────────────
def _extract_all_text(obj):
    """递归提取 JSON 中所有字符串值，拼接为文本"""
    texts = []
    if isinstance(obj, dict):
        for v in obj.values():
            texts.extend(_extract_all_text(v))
    elif isinstance(obj, list):
        for item in obj:
            texts.extend(_extract_all_text(item))
    elif isinstance(obj, str):
        texts.append(obj)
    return texts


def _count_keyword(text, keyword):
    """在 text 中统计 keyword 出现次数（不区分大小写，子串匹配）"""
    lower_text = text.lower()
    lw = keyword.lower()
    count = 0
    pos = 0
    while True:
        f = lower_text.find(lw, pos)
        if f == -1:
            break
        count += 1
        pos = f + 1
    return count


def _tokenize_words(text):
    """英文/中文分词，返回词数"""
    import re as _re
    tokens = _re.findall(r'[a-zA-Z0-9_-]+|[\u4e00-\u9fa5]', text)
    return len(tokens)


def _validate_seo_content(parsed_json, core_kw, secondary_kw_str, density_target, density_target_sec):
    """
    校验生成的 SEO 文案是否满足关键词密度要求。
    返回 (passed: bool, report: dict)
    """
    all_text = ' '.join(_extract_all_text(parsed_json))
    total_words = _tokenize_words(all_text)

    # 核心关键词
    core_count = _count_keyword(all_text, core_kw) if core_kw else 0
    core_density = (core_count / total_words * 100) if total_words > 0 else 0

    # 次要关键词
    sec_keywords = [k.strip() for k in secondary_kw_str.replace('\n', ',').split(',') if k.strip()]
    sec_details = []
    sec_total_count = 0
    for kw in sec_keywords:
        cnt = _count_keyword(all_text, kw)
        sec_total_count += cnt
        sec_details.append({'keyword': kw, 'count': cnt, 'status': 'ok' if cnt >= 1 else 'missing'})
    sec_total_density = (sec_total_count / total_words * 100) if total_words > 0 else 0

    # 判断是否通过
    core_ok = abs(core_density - density_target) <= 0.5
    # 次要关键词：每个至少出现 1 次
    sec_all_present = all(d['count'] >= 1 for d in sec_details) if sec_details else True
    # 次要关键词总密度在 ±0.5% 范围内
    sec_density_ok = abs(sec_total_density - density_target_sec) <= 0.5 if sec_keywords else True

    passed = core_ok and sec_all_present and sec_density_ok

    issues = []
    if not core_ok:
        issues.append(f'核心关键词 "{core_kw}" 密度 {core_density:.2f}% 不在目标 {density_target}%±0.5% 范围内')
    missing_sec = [d['keyword'] for d in sec_details if d['count'] == 0]
    if missing_sec:
        issues.append(f'以下次要关键词未出现: {", ".join(missing_sec)}')
    if not sec_density_ok and sec_keywords:
        issues.append(f'次要关键词总密度 {sec_total_density:.2f}% 不在目标 {density_target_sec}%±0.5% 范围内')

    report = {
        'passed': passed,
        'totalWords': total_words,
        'core': {
            'keyword': core_kw,
            'count': core_count,
            'density': round(core_density, 2),
            'target': density_target,
            'status': 'ok' if core_ok else 'fail'
        },
        'secondary': sec_details,
        'secondaryTotal': {
            'count': sec_total_count,
            'density': round(sec_total_density, 2),
            'target': density_target_sec,
            'status': 'ok' if sec_density_ok else 'fail'
        },
        'issues': issues,
    }
    return passed, report


def _build_correction_prompt(original_prompt, validation_report, parsed_json):
    """基于校验报告构建修正提示词，让 AI 定向修正不达标部分"""
    issues = validation_report.get('issues', [])
    issue_text = '\n'.join(f'  - {issue}' for issue in issues)
    core = validation_report.get('core', {})
    sec = validation_report.get('secondary', [])
    sec_total = validation_report.get('secondaryTotal', {})

    correction_details = f"""
【自检结果 - 未通过，需要修正】
当前问题：
{issue_text}

当前统计：
- 总词数: {validation_report.get('totalWords', 0)}
- 核心关键词 "{core.get('keyword', '')}" 出现 {core.get('count', 0)} 次，密度 {core.get('density', 0)}%，目标 {core.get('target', 0)}%
- 次要关键词总出现 {sec_total.get('count', 0)} 次，总密度 {sec_total.get('density', 0)}%，目标 {sec_total.get('target', 0)}%
"""
    missing = [d['keyword'] for d in sec if d['count'] == 0]
    if missing:
        correction_details += f'- 完全缺失的次要关键词: {", ".join(missing)}\n'
        correction_details += '- 缺失关键词的建议插入位置：\n'
        for i, kw in enumerate(missing):
            suggestions = []
            if i % 3 == 0:
                suggestions.append(f'FAQ 的某个问题或回答中融入 "{kw}"')
            if i % 3 == 1:
                suggestions.append(f'Features 或 Showcase 的某个 description 中融入 "{kw}"')
            if i % 3 == 2:
                suggestions.append(f'WhyChooseUs 或 HowItWork 的 description 中融入 "{kw}"')
            suggestions.append(f'任何 section 的 description 字段中自然融入 "{kw}"')
            correction_details += f'  - "{kw}": {"; 或 ".join(suggestions)}\n'

    correction_details += f"""
【修正要求 — 必须逐个检查次要关键词】
1. 保持原有 JSON 结构和所有字段不变
2. **逐个检查**以下次要关键词是否出现，缺失的必须补入：
{chr(10).join(f'   - "{d["keyword"]}": {"✅已出现"+str(d["count"])+"次" if d["count"]>0 else "❌缺失，必须在某个description/answer/title中融入"}' for d in sec)}
3. 针对以上问题进行定向修正：
   - 如果核心关键词密度不足，在描述性文本中自然增加使用
   - 如果核心关键词密度过高，用同义表达替换部分出现
   - **缺失的次要关键词必须融入到 FAQ回答、Features描述、Showcase描述 等字段中**
   - FAQ的问题和回答是埋入次要关键词的最佳位置
4. 修改时保证语义自然，不要关键词堆砌
5. 不要改变 JSON 键名，只修改值
6. 直接输出修正后的完整 JSON，不要输出说明文字

以下是需要修正的上一版内容：
"""
    return original_prompt + correction_details + json.dumps(parsed_json, ensure_ascii=False, indent=2)


def _parse_ai_content(content, mode, sections, include_metadata, rewrite_section_id=None):
    """解析 AI 返回内容"""
    clean = _strip_code_fence(content)

    if mode == 'rewrite':
        return {rewrite_section_id: clean}

    # 生成模式：优先 JSON，降级 regex 提取
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[\s\S]*\}', clean)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    # Markdown 分隔符降级
    expected_ids = []
    if include_metadata:
        expected_ids.append('seo_metadata')
    expected_ids.extend(s['id'] for s in sections)
    result = {}
    parts = re.split(r'===SECTION:\s*([a-zA-Z0-9_]+)\s*===', clean)
    for i in range(1, len(parts), 2):
        sid = parts[i].strip()
        body_text = parts[i + 1].strip() if i + 1 < len(parts) else ''
        if sid in expected_ids:
            result[sid] = body_text
    if not result and expected_ids:
        result[expected_ids[0]] = clean
    return result


def _try_parse_sse_response(raw_text):
    """尝试从 SSE 流式响应（含 :PING 等）中提取 JSON 数据"""
    # 移除 SSE 前缀行（:PING, data: 等）并尝试找到完整的 JSON 对象
    # 常见格式: ": PING\n: PING\n{\"id\":\"msg_...\", ...}"
    # 或: "data: {\"id\":...}\n\ndata: [DONE]"
    lines = raw_text.split('\n')
    json_parts = []
    for line in lines:
        stripped = line.strip()
        # 跳过 SSE 注释行和空行
        if not stripped or stripped.startswith(':'):
            continue
        # 去掉 SSE data: 前缀
        if stripped.startswith('data:'):
            stripped = stripped[5:].strip()
        if stripped == '[DONE]':
            continue
        json_parts.append(stripped)

    combined = '\n'.join(json_parts)
    if not combined:
        return None

    # 尝试直接解析
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        pass

    # 尝试提取第一个完整的 JSON 对象
    m = re.search(r'\{[\s\S]*\}', combined)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _call_apimart(api_key, prompt, model='gemini-3.1-pro-preview', temperature=0.3):
    """调用 apimart.ai 聊天接口"""
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'temperature': temperature,
        'max_tokens': 20000,
    }
    resp = requests.post(APIMART_URL, json=payload, headers={
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }, timeout=120)

    raw = resp.text
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 有时 API 会返回 SSE 流式格式（带 :PING 前缀），尝试从中提取 JSON
        sse_json = _try_parse_sse_response(raw)
        if sse_json is not None:
            data = sse_json
        else:
            code_m = re.search(r'error code:\s*(\d+)', raw, re.I)
            code = code_m.group(1) if code_m else None
            error_map = {
                '524': '上游 API 超时（524），请稍后重试',
                '523': '上游 API 不可达（523）',
                '522': '上游 API 连接超时（522）',
                '503': '上游服务不可用（503）',
                '502': '上游网关错误（502）',
            }
            msg = error_map.get(code, f'Non-JSON response (HTTP {resp.status_code}): {raw[:200]}')
            return None, msg

    if not resp.ok or data.get('error'):
        err = data.get('error', '')
        if isinstance(err, dict):
            err = err.get('message', json.dumps(err))
        return None, str(err) or f'HTTP {resp.status_code}'

    content = ''
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError):
        pass
    if not content:
        try:
            content = data['data']['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            pass
    if not content:
        return None, 'AI 返回内容为空'
    return content, None


@app.route('/api/seo/generate', methods=['POST', 'OPTIONS'])
def seo_generate():
    """SEO 文案生成 — 带服务端关键词密度校验与自动重试"""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(force=True)
    api_key = (data or {}).get('apiKey', '')
    if not api_key:
        return jsonify({'error': '缺少 apiKey'}), 400
    if not (data.get('coreKeyword') or '').strip():
        return jsonify({'error': '缺少核心关键词'}), 400
    if not data.get('sections'):
        return jsonify({'error': '缺少 sections'}), 400

    MAX_ATTEMPTS = 3
    core_kw = data.get('coreKeyword', '').strip()
    secondary_kw = data.get('secondaryKeywords', '')
    density_target = float(data.get('densityTarget', 2.0))
    density_target_sec = float(data.get('densityTargetSecondary', 3.0))

    try:
        # 风格爬取
        data = _enrich_style_samples(data)
        # 构建初始提示词
        base_prompt = _build_generation_prompt(data)
        prompt = base_prompt
        model = data.get('model', 'gemini-3.1-pro-preview')

        result = None
        validation_report = None
        last_err = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f'[seo/generate] 第 {attempt}/{MAX_ATTEMPTS} 次生成...', flush=True)

            content, err = _call_apimart(api_key, prompt, model=model)
            if err:
                last_err = err
                print(f'[seo/generate] AI 调用失败: {err}', flush=True)
                break

            # 解析结果
            parsed = _parse_ai_content(
                content, 'generate',
                data.get('sections', []),
                data.get('includeMetadata', False),
            )

            if not isinstance(parsed, dict) or not parsed:
                last_err = 'AI 返回内容无法解析为有效 JSON'
                print(f'[seo/generate] 解析失败，第 {attempt} 次', flush=True)
                continue

            # 服务端密度校验
            passed, report = _validate_seo_content(
                parsed, core_kw, secondary_kw, density_target, density_target_sec
            )
            validation_report = report
            result = parsed

            print(f'[seo/generate] 第 {attempt} 次校验: passed={passed}, '
                  f'core={report["core"]["density"]}% (target {density_target}%), '
                  f'secTotal={report["secondaryTotal"]["density"]}% (target {density_target_sec}%), '
                  f'issues={report["issues"]}', flush=True)

            if passed:
                # 通过校验，添加元数据并返回
                result['_validation'] = {
                    'attempts_used': attempt,
                    'passed': True,
                    'report': validation_report,
                }
                return jsonify(result), 200

            # 未通过：基于原始 prompt 构建修正提示词（避免每次叠加膨胀）
            if attempt < MAX_ATTEMPTS:
                prompt = _build_correction_prompt(base_prompt, report, parsed)

        # 所有尝试都未通过（或全部 AI 调用失败）
        if result is not None:
            # 有结果但未通过校验 → 仍然返回，附带失败状态
            result['_validation'] = {
                'attempts_used': MAX_ATTEMPTS,
                'passed': False,
                'report': validation_report,
            }
            return jsonify(result), 200
        else:
            return jsonify({'error': last_err or '生成失败'}), 502

    except requests.Timeout:
        return jsonify({'error': 'API 调用超时，请稍后重试'}), 504
    except Exception as e:
        return jsonify({'error': f'生成失败: {str(e)}'}), 500


@app.route('/api/seo/rewrite', methods=['POST', 'OPTIONS'])
def seo_rewrite():
    """SEO 文案重写 — 直接调用 apimart.ai"""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(force=True)
    api_key = (data or {}).get('apiKey', '')
    if not api_key:
        return jsonify({'error': '缺少 apiKey'}), 400

    try:
        data = _enrich_style_samples(data)
        prompt = _build_rewrite_prompt(data)
        model = data.get('model', 'gemini-3.1-pro-preview')
        content, err = _call_apimart(api_key, prompt, model=model)
        if err:
            return jsonify({'error': err}), 502
        result = _parse_ai_content(
            content, 'rewrite',
            data.get('sections', []),
            False,
            rewrite_section_id=data.get('rewriteSectionId', ''),
        )
        return jsonify(result), 200
    except requests.Timeout:
        return jsonify({'error': 'API 调用超时，请稍后重试'}), 504
    except Exception as e:
        return jsonify({'error': f'重写失败: {str(e)}'}), 500


# ══════════════════════════════════════════════════════════════
# Section 图片生成 — 生成提示词 + 调用生图 API + 打包 ZIP
# ══════════════════════════════════════════════════════════════

# 非 showcase 的提示词生成 system prompt
_IMGPROMPT_SYSTEM_GENERAL = """请根据我提供的 section 文案，生成适合"填色页网站配图"的英文生图提示词。

要求：
1. 不是普通 SaaS 配图，而是填色页网站风格配图
2. 参考常见视觉方向：可爱、清晰、儿童友好、创意感、网页友好、轻 UI 感
3. 如果 section 是功能模块，要突出功能结果
4. 如果 section 是 hero，要更有吸引力和主视觉感
5. 输出格式必须是：
   - Section Type
   - Section Goal
   - Image Prompt
   - Negative Prompt
6. 只输出英文，不要解释
7. 不要出现 watermark、logo、readable text、杂乱背景
8. 需要生成彩色的图片"""

# showcase 专用的提示词生成 system prompt
_IMGPROMPT_SYSTEM_SHOWCASE = """请根据我提供的 section 文案，生成适合"填色页网站配图"的英文生图提示词。

你必须严格遵守以下规则：
1. 先理解文案，理解文案需要表达的画面
2. 严禁加入以下内容：
   - 任何颜色描述，例如红色、蓝色、金色、彩色等
   - 任何光影描述，例如发光、明亮、阴影、渐变、反光、照明、氛围光等
   - 任何剪影类描述
   - 任何灰度、上色、纹理渲染、材质光泽、3D、写实打光等内容

3. 最终只输出一条英文 prompt，不要输出解释，不要输出标题，不要输出多段内容。

最终输出的 prompt 必须严格使用下面这个结构：
Create a clean digital vector line art illustration of {文案主题}. Style: low complexity, bold distinct black outlines, no thin lines, strictly monochrome, clear forms with pure white uncolored interiors, strictly no shading or gray tones, no solid color fill, ensure the whole subject is fully visible, isolated on a pure white background."""


def _generate_image_prompt(api_key, section_type, section_content, is_showcase=False):
    """调用 claude-haiku-4-5 生成生图提示词"""
    system = _IMGPROMPT_SYSTEM_SHOWCASE if is_showcase else _IMGPROMPT_SYSTEM_GENERAL
    user_msg = f"Section Type: {section_type}\n\n文案内容：\n{section_content}"

    payload = {
        'model': 'claude-haiku-4-5-20251001',
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user_msg},
        ],
        'stream': False,
        'temperature': 0.4,
        'max_tokens': 1000,
    }
    resp = requests.post(APIMART_URL, json=payload, headers={
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }, timeout=60)

    raw = resp.text
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sse_json = _try_parse_sse_response(raw)
        if sse_json is not None:
            data = sse_json
        else:
            return None, f'提示词生成返回无法解析: {raw[:200]}'

    if not resp.ok or data.get('error'):
        err = data.get('error', '')
        if isinstance(err, dict):
            err = err.get('message', json.dumps(err))
        return None, str(err) or f'HTTP {resp.status_code}'

    content = ''
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError):
        pass
    if not content:
        try:
            content = data['data']['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            pass
    if not content:
        return None, f'提示词生成返回为空, raw keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}'

    # 对于非 showcase，从返回中提取 Image Prompt 行
    if not is_showcase:
        # 提取 Image Prompt 和 Negative Prompt
        image_prompt = ''
        negative_prompt = ''
        for line in content.split('\n'):
            stripped = line.strip()
            if stripped.lower().startswith('image prompt'):
                image_prompt = stripped.split(':', 1)[-1].strip().strip('"').strip("'")
            elif stripped.lower().startswith('negative prompt'):
                negative_prompt = stripped.split(':', 1)[-1].strip().strip('"').strip("'")
        if not image_prompt:
            # fallback: 用整个返回作为 prompt
            image_prompt = content.strip()
        return {'image_prompt': image_prompt, 'negative_prompt': negative_prompt}, None
    else:
        # showcase: 整个返回就是 prompt
        return {'image_prompt': content.strip(), 'negative_prompt': ''}, None


def _submit_image_task(api_key, prompt, negative_prompt='', size='1:1'):
    """提交生图任务到 apimart，返回 task_id"""
    full_prompt = prompt
    if negative_prompt:
        full_prompt += f'\n\nNegative: {negative_prompt}'

    resp = requests.post(
        'https://api.apimart.ai/v1/images/generations',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': 'gemini-3.1-flash-image-preview',
            'prompt': full_prompt,
            'size': size,
            'resolution': '1K',
            'n': 1,
        },
        timeout=120,
    )
    body = resp.json()
    if not resp.ok:
        err = body.get('error', '')
        if isinstance(err, dict):
            err = err.get('message', json.dumps(err))
        return None, str(err) or f'HTTP {resp.status_code}'

    # 提取 task_id
    task_id = None
    if body.get('data') and isinstance(body['data'], list) and len(body['data']) > 0:
        task_id = body['data'][0].get('task_id')
    if not task_id:
        task_id = body.get('task_id') or body.get('id')
    if not task_id:
        return None, f'未获取到 task_id: {json.dumps(body)[:300]}'
    return task_id, None


def _poll_image_task(api_key, task_id, max_attempts=90, interval=3):
    """轮询图片任务直到完成或失败，返回图片 URL"""
    import time as _time
    for _ in range(max_attempts):
        _time.sleep(interval)
        try:
            resp = requests.get(
                f'https://api.apimart.ai/v1/tasks/{task_id}?language=zh',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=30,
            )
            body = resp.json()
        except Exception as e:
            print(f'[imggen/poll] task {task_id} 请求失败: {e}', flush=True)
            continue

        status = (body.get('status') or body.get('data', {}).get('status') or '').lower()
        if status in ('completed', 'success', 'succeeded', 'finished', 'done'):
            # 提取图片 URL
            task_data = body.get('data', {}).get('result') or body.get('data', {}).get('output') or body.get('data', {}) or body
            img_url = None
            if isinstance(task_data, dict):
                images = task_data.get('images', [])
                if images and isinstance(images, list):
                    first = images[0]
                    if isinstance(first, dict):
                        img_url = first.get('url')
                        if isinstance(img_url, list):
                            img_url = img_url[0] if img_url else None
                        if not img_url and first.get('b64_json'):
                            img_url = 'data:image/png;base64,' + first['b64_json']
                if not img_url:
                    img_url = task_data.get('image_url') or task_data.get('url')
                    if isinstance(img_url, list):
                        img_url = img_url[0] if img_url else None
            if not img_url:
                return None, f'任务完成但未找到图片 URL'
            return img_url, None
        elif status in ('failed', 'fail', 'error', 'cancelled'):
            err_msg = body.get('error', {})
            if isinstance(err_msg, dict):
                err_msg = err_msg.get('message', '任务失败')
            return None, str(err_msg) or '任务失败'

    return None, '轮询超时（约4.5分钟）'


def _download_image(url, api_key=None):
    """下载图片，返回 bytes"""
    if url.startswith('data:'):
        _, b64 = url.split(',', 1)
        return base64.b64decode(b64)
    headers = {}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.content


# ── KIE.ai 图片生成 ──
KIE_API_BASE = 'https://api.kie.ai'
KIE_FILE_UPLOAD_BASE = 'https://kieai.redpandaai.co'


def _submit_kie_image_task(api_key, prompt, size='1:1'):
    """提交生图任务到 KIE.ai (nano-banana-2)，返回 task_id"""
    resp = requests.post(
        f'{KIE_API_BASE}/api/v1/jobs/createTask',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': 'nano-banana-2',
            'input': {
                'prompt': prompt,
                'aspect_ratio': size,
                'resolution': '1K',
                'output_format': 'jpg',
            },
        },
        timeout=120,
    )
    body = resp.json()
    print(f'[imggen/kie-submit] response: {str(body)[:500]}', flush=True)
    if body.get('code') != 200:
        return None, body.get('msg', 'KIE 任务创建失败')
    task_id = (body.get('data') or {}).get('taskId')
    if not task_id:
        return None, f'未获取到 taskId: {json.dumps(body)[:300]}'
    return task_id, None


def _poll_kie_image_task(api_key, task_id, max_attempts=90, interval=3):
    """轮询 KIE 图片任务直到完成或失败，返回图片 URL"""
    import time as _time
    for attempt in range(max_attempts):
        _time.sleep(interval)
        try:
            resp = requests.get(
                f'{KIE_API_BASE}/api/v1/jobs/recordInfo',
                params={'taskId': task_id},
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=30,
            )
            body = resp.json()
        except Exception as e:
            print(f'[imggen/kie-poll] task {task_id} 请求失败: {e}', flush=True)
            continue

        task_data = body.get('data', {})
        state = (task_data.get('state') or '').lower()
        print(f'[imggen/kie-poll] attempt {attempt+1}, state={state}', flush=True)

        if state == 'success':
            result_json_str = task_data.get('resultJson', '')
            try:
                result_json = json.loads(result_json_str) if result_json_str else {}
            except json.JSONDecodeError:
                return None, f'KIE resultJson 解析失败: {result_json_str[:200]}'
            urls = result_json.get('resultUrls', [])
            if urls:
                return urls[0], None
            return None, '任务完成但未找到图片 URL'
        elif state == 'fail':
            return None, task_data.get('failMsg') or '任务失败'
        # waiting/queuing/generating → continue polling

    return None, '轮询超时（约4.5分钟）'


def _upload_to_kie(api_key, file_url, upload_path='seo-images', file_name=None):
    """通过 URL 上传文件到 KIE 文件存储"""
    payload = {
        'fileUrl': file_url,
        'uploadPath': upload_path,
    }
    if file_name:
        payload['fileName'] = file_name
    resp = requests.post(
        f'{KIE_FILE_UPLOAD_BASE}/api/file-url-upload',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json=payload,
        timeout=60,
    )
    body = resp.json()
    if not body.get('success'):
        return None, body.get('msg', 'KIE 文件上传失败')
    return body.get('data', {}).get('downloadUrl') or body.get('data', {}).get('fileUrl'), None


@app.route('/api/seo/generate-image', methods=['POST', 'OPTIONS'])
def seo_generate_image():
    """单个 Section 图片生成 — 生成提示词 → 生图 → 返回 URL"""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(force=True)
    api_key = (data or {}).get('apiKey', '')  # apimart key（提示词生成）
    if not api_key:
        return jsonify({'error': '缺少 apiKey'}), 400

    provider = data.get('provider', 'apimart')
    kie_api_key = data.get('kieApiKey', '')
    if provider == 'kie' and not kie_api_key:
        return jsonify({'error': '使用 KIE 平台需要提供 kieApiKey'}), 400

    section_id = data.get('sectionId', '')
    index = data.get('index', 0)
    label = data.get('label', f'{section_id}_{index}')
    content = data.get('content', '')
    is_showcase = (section_id == 'showcase')

    # Step 1: 生成提示词（始终用 apimart + claude-haiku-4-5）
    print(f'[seo/gen-img] 生成提示词: {label} (provider={provider})', flush=True)
    prompt_result, err = _generate_image_prompt(api_key, section_id, content, is_showcase)
    if err:
        return jsonify({'error': f'提示词生成失败: {err}'}), 500

    image_prompt = prompt_result['image_prompt']
    negative_prompt = prompt_result.get('negative_prompt', '')
    print(f'[seo/gen-img] 提示词: {image_prompt[:120]}...', flush=True)

    # Step 2 & 3: 生图（根据 provider 走不同通道）
    # Apimart 仅支持 1:1,3:4,4:3,9:16,16:9
    # KIE 支持更多比例（含 3:2,21:9 等）
    # What Is → 宽幅横图；其他 → 中等横图
    if section_id == 'whatIs':
        size = '16:9' if provider != 'kie' else '21:9'
    else:
        size = '4:3' if provider != 'kie' else '3:2'

    if provider == 'kie':
        task_id, err = _submit_kie_image_task(kie_api_key, image_prompt, size=size)
        if err:
            return jsonify({'error': f'KIE 生图任务提交失败: {err}', 'prompt': image_prompt}), 500
        print(f'[seo/gen-img/kie] task_id: {task_id}', flush=True)
        img_url, err = _poll_kie_image_task(kie_api_key, task_id)
    else:
        task_id, err = _submit_image_task(api_key, image_prompt, negative_prompt, size=size)
        if err:
            return jsonify({'error': f'生图任务提交失败: {err}', 'prompt': image_prompt}), 500
        print(f'[seo/gen-img] task_id: {task_id}', flush=True)
        img_url, err = _poll_image_task(api_key, task_id)

    if err:
        return jsonify({'error': f'生图失败: {err}', 'prompt': image_prompt}), 500

    print(f'[seo/gen-img] 完成: {img_url[:80]}', flush=True)
    return jsonify({
        'imageUrl': img_url,
        'prompt': image_prompt,
        'sectionId': section_id,
        'index': index,
    })


@app.route('/api/seo/pack-images', methods=['POST', 'OPTIONS'])
def seo_pack_images():
    """打包已生成的图片为 ZIP"""
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(force=True)
    api_key = (data or {}).get('apiKey', '')
    items = data.get('items', [])
    core_keyword = data.get('coreKeyword', '')

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, item in enumerate(items):
            img_url = item.get('imageUrl', '')
            if not img_url:
                continue
            try:
                img_bytes = _download_image(img_url, api_key)
                fname = f"{idx+1:02d}_{item.get('sectionId', '')}_{item.get('index', 0)}.png"
                zf.writestr(fname, img_bytes)
            except Exception as e:
                print(f'[pack] 下载失败: {e}', flush=True)

        prompt_lines = []
        for idx, item in enumerate(items):
            prompt_lines.append(f"[{idx+1:02d}] {item.get('label', '')}")
            if item.get('prompt'):
                prompt_lines.append(f"    Prompt: {item['prompt']}")
            prompt_lines.append('')
        zf.writestr('_prompts.txt', '\n'.join(prompt_lines))

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'seo_images_{core_keyword.replace(" ", "_")}_{int(__import__("time").time())}.zip',
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    print(f'\n[OK] 后端已启动: http://0.0.0.0:{port}')
    print('[提示] 如遇 Cloudflare 保护网站，将自动启动浏览器引擎处理\n')
    app.run(host='0.0.0.0', debug=False, port=port)







