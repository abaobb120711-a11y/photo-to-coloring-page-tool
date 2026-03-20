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
    kwargs = dict(timeout=timeout, stream=stream, allow_redirects=True)
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

    # 构建 API URL
    api_base = f"{parsed.scheme}://{parsed.netloc}/api.php"
    try:
        resp = _session.get(api_base, params={
            'action': 'parse', 'page': page_name,
            'prop': 'text', 'format': 'json'
        }, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if 'parse' not in data or 'text' not in data['parse']:
            return None
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
    """清理 Fandom/Wikia 等网站的缩略图 URL，获取高清原图。"""
    url = re.sub(r'/revision/latest/scale-to-width-down/\d+', '/revision/latest', url)
    url = re.sub(r'/revision/latest/smart-width/\d+', '/revision/latest', url)
    url = re.sub(r'\?cb=\d+&(path-prefix=[^&]+&)?width=\d+.*$', '', url)
    url = re.sub(r'\?width=\d+.*$', '', url)
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
        src = clean_image_url(src)
        if src in seen:
            return
        seen.add(src)
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


# ── /scrape ──────────────────────────────────────────────────────
@app.route('/scrape', methods=['POST', 'OPTIONS'])
def scrape():
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL 为空'}), 400

    # 先用 requests 快速抓取
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
        if images:
            return jsonify(images)

    # requests 没拿到图片 → 尝试 MediaWiki API（Fandom/Wikia 等）
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
        r = do_request(image_url, stream=True, timeout=15)
        ct = r.headers.get('Content-Type', 'image/jpeg')
        return Response(r.content, content_type=ct)
    except Exception:
        return Response(b'', content_type='image/png', status=200)


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
            ct = r.headers.get('Content-Type', 'image/png').split(';')[0].strip()
            if ct == 'application/octet-stream':
                ct = 'image/png'
            b64 = base64.b64encode(img_bytes).decode('utf-8')
            image_data = f"data:{ct};base64,{b64}"
        except Exception as e:
            return jsonify({'error': f'下载图片失败: {e}'}), 500
    else:
        image_data = image_src  # 已经是 base64 data URI
        # 从 data URI 解码出原始字节以检测尺寸
        try:
            header, b64_str = image_data.split(',', 1)
            img_bytes = base64.b64decode(b64_str)
        except Exception:
            pass

    # 检测图片尺寸，自动选择最接近原图的输出比例
    size = '1:1'
    if img_bytes:
        try:
            pil_img = PILImage.open(io.BytesIO(img_bytes))
            w, h = pil_img.size
            size = best_aspect_ratio(w, h)
        except Exception:
            pass

    try:
        resp = requests.post(
            'https://api.apimart.ai/v1/images/generations',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'gemini-3.1-flash-image-preview',
                'prompt': COLORING_PROMPT,
                'image_urls': [image_data],
                'size': size,
                'resolution': '1K',
                'n': 1
            },
            timeout=120
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({'error': f'API 请求失败: {e}'}), 500


@app.route('/api/task/<task_id>', methods=['GET', 'OPTIONS'])
def api_task(task_id):
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    api_key = request.args.get('apiKey', '').strip()
    if not api_key:
        return jsonify({'error': 'API Key 不能为空'}), 400
    try:
        resp = requests.get(
            f'https://api.apimart.ai/v1/tasks/{task_id}?language=zh',
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30
        )
        return jsonify(resp.json()), resp.status_code
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    print(f'\n[OK] 后端已启动: http://0.0.0.0:{port}')
    print('[提示] 如遇 Cloudflare 保护网站，将自动启动浏览器引擎处理\n')
    app.run(host='0.0.0.0', debug=False, port=port)


