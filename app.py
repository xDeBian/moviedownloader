from flask import Flask, render_template, request, jsonify, Response, send_file
import requests
import cloudscraper
from bs4 import BeautifulSoup
import re
import subprocess
import os
import json
import threading
from threading import Lock
import uuid
import shutil
from datetime import datetime

# yt-dlp-ის სრული გზა (PATH-ში არ არის Mac-ზე)
YT_DLP = shutil.which('yt-dlp') or os.path.expanduser('~/Library/Python/3.9/bin/yt-dlp')

app = Flask(__name__)

# Cloudflare bypass scraper
scraper = cloudscraper.create_scraper()

# Store active download jobs
downloads = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://mykadri.tv/",
    "Accept-Language": "ka-GE,ka;q=0.9,en;q=0.8",
}

DOWNLOAD_DIR = os.path.expanduser("~/Downloads/KadriMovies")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

JOBS_FILE = os.path.join(os.path.dirname(__file__), 'jobs.json')
jobs_lock = Lock()


def load_jobs_file():
    try:
        with open(JOBS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_jobs_file(data):
    with jobs_lock:
        with open(JOBS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def update_job_status(job_id, status, progress=None, filename=None, error=None):
    jobs_data = load_jobs_file()
    if job_id in jobs_data:
        jobs_data[job_id]['status'] = status
        if progress is not None:
            jobs_data[job_id]['progress'] = progress
        if filename:
            jobs_data[job_id]['filename'] = filename
        if error:
            jobs_data[job_id]['error'] = error
        save_jobs_file(jobs_data)


def _extract_balanced(text, start):
    """Extract balanced {..} or [..] starting at `start` index."""
    open_c = text[start]
    close_c = '}' if open_c == '{' else ']'
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == '\\' and in_str:
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if not in_str:
            if c == open_c:
                depth += 1
            elif c == close_c:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def extract_jwplayer_sources(html_content, page_url):
    """
    Parse mykadri.tv page and return:
      sources  - flat list of m3u8 URLs (movies / fallback)
      labels   - corresponding lang labels
      iframes  - iframe src list
      seasons  - dict {season_str: [{title, ep_num, sources:[{url,lang}]}]} for series
    """
    sources, labels, iframes = [], [], []
    seasons = {}

    soup = BeautifulSoup(html_content, 'html.parser')

    for script in soup.find_all('script'):
        text = script.string
        if not text:
            continue

        # ── series playlist: {"1":[...], "2":[...]} ─────────────────────────
        pl_match = re.search(r'\bplaylist\s*:\s*([{\[])', text)
        if pl_match:
            start = pl_match.start(1)
            raw = _extract_balanced(text, start)
            if raw:
                try:
                    pl = json.loads(raw)
                    if isinstance(pl, dict):
                        # Numeric keys only (season numbers)
                        digit_keys = sorted([k for k in pl.keys() if k.isdigit()], key=int)
                        if not digit_keys:
                            # Non-numeric keys: treat as flat sources
                            for item in pl.values():
                                if isinstance(item, list):
                                    for src in item:
                                        if isinstance(src, dict):
                                            f = src.get('file', '')
                                            if f and f not in sources:
                                                sources.append(f)
                                                labels.append(src.get('label', ''))
                        else:
                            # Decide: real series (has 'languages' sub-key OR multiple seasons)
                            # vs movie with language variants (single key, direct 'sources' in items)
                            first_eps = pl.get(digit_keys[0], [])
                            has_languages = any(ep.get('languages') for ep in first_eps if isinstance(ep, dict))
                            multiple_seasons = len(digit_keys) > 1

                            if has_languages or multiple_seasons:
                                # Real multi-season series
                                for season_key in digit_keys:
                                    eps = pl[season_key]
                                    season_eps = []
                                    for i, ep in enumerate(eps):
                                        ep_sources = []
                                        for lang in ep.get('languages', []):
                                            for src in lang.get('sources', []):
                                                f = src.get('file', '')
                                                if f and f not in [s['url'] for s in ep_sources]:
                                                    ep_sources.append({'url': f, 'lang': lang.get('label', '')})
                                        # Also try direct sources (fallback)
                                        if not ep_sources:
                                            for src in ep.get('sources', []):
                                                f = src.get('file', '')
                                                if f and f not in [s['url'] for s in ep_sources]:
                                                    ep_sources.append({'url': f, 'lang': ep.get('title', '')})
                                        if ep_sources:
                                            season_eps.append({
                                                'title': ep.get('title', f'სერია {i+1}'),
                                                'ep_num': i + 1,
                                                'sources': ep_sources,
                                            })
                                    if season_eps:
                                        seasons[season_key] = season_eps
                                # Flat sources from S1E1 for backwards-compat
                                first_season = digit_keys[0] if seasons else None
                                if first_season and seasons.get(first_season):
                                    for s in seasons[first_season][0]['sources']:
                                        if s['url'] not in sources:
                                            sources.append(s['url'])
                                            labels.append(s['lang'])
                            else:
                                # Movie with language variant items (single season, no 'languages' sub-key)
                                for ep in first_eps:
                                    if not isinstance(ep, dict):
                                        continue
                                    lbl = ep.get('title', ep.get('label', ''))
                                    for src in ep.get('sources', []):
                                        f = src.get('file', '')
                                        if f and f not in sources:
                                            sources.append(f)
                                            labels.append(lbl)
                    elif isinstance(pl, list):
                        # Movie / single-lang array
                        for item in pl:
                            for src in item.get('sources', []):
                                f = src.get('file', '')
                                if f and f not in sources:
                                    sources.append(f)
                                    labels.append(item.get('label', ''))
                except Exception:
                    pass

        # ── movie: [...] fallback ────────────────────────────────────────────
        if not sources and not seasons:
            movie_m = re.search(r'\bmovie\s*:\s*\[', text)
            if movie_m:
                raw = _extract_balanced(text, movie_m.end() - 1)
                if raw:
                    try:
                        for item in json.loads(raw):
                            for src in item.get('sources', []):
                                f = src.get('file', '')
                                if f and f not in sources:
                                    sources.append(f)
                                    labels.append(item.get('label', ''))
                    except Exception:
                        pass

        # ── generic m3u8 / mp4 fallback ──────────────────────────────────────
        if not sources and not seasons:
            for pat in [r'["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)["\']?',
                        r'["\']?(https?://[^"\'>\s]+\.mp4[^"\'>\s]*)["\']?']:
                for m in re.finditer(pat, text):
                    u = m.group(1)
                    if u not in sources:
                        sources.append(u)
                        labels.append('')

    # ── iframes ──────────────────────────────────────────────────────────────
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src') or iframe.get('data-lazy', '')
        if src and src.startswith('http'):
            iframes.append(src)

    return sources, labels, iframes, seasons


def scrape_page(url):
    """Scrape the movie/series page and extract video info (mykadri.tv specific)"""
    try:
        resp = scraper.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, 'html.parser')

        # ── სათაური ─────────────────────────────────────────────────────────
        title = ""
        h1 = soup.find('h1', class_=lambda c: c and 'movie__title' in c)
        if h1:
            title = h1.get_text(strip=True)
        if not title:
            og = soup.find('meta', property='og:title')
            title = og['content'].replace(' - MyKadri.Tv', '').strip() if og else ''

        # ── პოსტერი ──────────────────────────────────────────────────────────
        poster = ""
        og_image = soup.find('meta', property='og:image')
        if og_image:
            poster = og_image.get('content', '')

        # ── სერიალი თუ ფილმი? ────────────────────────────────────────────────
        # mykadri.tv სერიალებს აქვს tab-ები "სეზონი X" ან ეპიზოდის ღილაკები
        is_series = False
        episodes = []

        # ეპიზოდების ლინკები — href-ში /serialebi_ ან epizodi პატერნი
        ep_pattern = re.compile(
            r'(serialebi|epizodi|sezona|season|episode|\bs\d+e\d+\b)',
            re.IGNORECASE
        )
        seen_ep_urls = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if ep_pattern.search(href + text) and href not in seen_ep_urls:
                full_url = href if href.startswith('http') else f"https://mykadri.tv{href}"
                episodes.append({'title': text or href, 'url': full_url})
                seen_ep_urls.add(href)

        # ასევე შევამოწმოთ URL თავად
        if ep_pattern.search(url) or episodes:
            is_series = bool(episodes)

        # ── ვიდეო სოურსების ამოღება ──────────────────────────────────────────
        sources, labels, iframes, seasons = extract_jwplayer_sources(html, url)

        # iframe პლეიერებიდანაც სცადოს (რუსული/ინგლისური ვერსიები)
        extra_players = []
        for iframe_url in iframes[:3]:
            try:
                fr = scraper.get(
                    iframe_url,
                    headers={**HEADERS, 'Referer': url},
                    timeout=10
                )
                fs, fl, _, _ = extract_jwplayer_sources(fr.text, iframe_url)
                for s, l in zip(fs, fl):
                    if s not in sources:
                        sources.append(s)
                        labels.append(l)
                        extra_players.append({'url': s, 'label': l, 'origin': iframe_url})
            except Exception:
                pass

        # ── სეზონი/ეპიზოდის ID (S01E01 ფორმატი, Plex naming) ────────────────
        episode_id = ''
        # URL-ში და სათაურში ვეძებთ
        for haystack in [url, title]:
            if episode_id:
                break
            # SxxExx ან sezona-X-epizodi-Y პატერნები
            for pat in [
                r'[Ss](\d+)[Ee](\d+)',
                r'sezona[- _]?(\d+)[- _]*epizodi[- _]?(\d+)',
                r'season[- _]?(\d+)[- _]*episode[- _]?(\d+)',
            ]:
                m = re.search(pat, haystack, re.IGNORECASE)
                if m:
                    episode_id = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
                    break
            # ქართული პატერნი სათაურში
            if not episode_id:
                m = re.search(r'სეზონი\s*(\d+)\s*ეპიზოდი\s*(\d+)', haystack)
                if m:
                    episode_id = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"

        return {
            'success': True,
            'title': title,
            'poster': poster,
            'is_series': is_series or bool(seasons),
            'episodes': episodes,
            'seasons': seasons,
            'sources': sources,
            'labels': labels,
            'iframes': iframes,
            'episode_id': episode_id,
            'url': url
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_yt_dlp_info(url, referer=None):
    """Use yt-dlp to extract video info"""
    cmd = [
        YT_DLP,
        '--dump-json',
        '--no-playlist',
        '--user-agent', HEADERS['User-Agent'],
    ]
    if referer:
        cmd += ['--referer', referer]
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            formats = []
            for f in data.get('formats', []):
                if f.get('vcodec') != 'none':
                    formats.append({
                        'format_id': f.get('format_id'),
                        'ext': f.get('ext'),
                        'resolution': f.get('resolution', f"{f.get('height', '?')}p"),
                        'filesize': f.get('filesize'),
                        'url': f.get('url'),
                        'note': f.get('format_note', '')
                    })
            return {
                'success': True,
                'title': data.get('title', ''),
                'thumbnail': data.get('thumbnail', ''),
                'duration': data.get('duration'),
                'formats': formats
            }
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'Timeout'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

    return {'success': False, 'error': result.stderr[:200]}


def get_hls_qualities(m3u8_url, referer=None):
    """m3u8 master playlist-იდან ხელმისაწვდომი ხარისხების ამოღება"""
    headers = {
        'User-Agent': HEADERS['User-Agent'],
        'Referer': referer or 'https://mykadri.tv/'
    }
    try:
        resp = requests.get(m3u8_url, headers=headers, timeout=10)
        resp.raise_for_status()
        base_url = m3u8_url.rsplit('/', 1)[0]
        qualities = []
        lines = resp.text.splitlines()
        for i, line in enumerate(lines):
            if not line.strip().startswith('#EXT-X-STREAM-INF:'):
                continue
            res_m = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            bw_m = re.search(r'BANDWIDTH=(\d+)', line)
            height = int(res_m.group(2)) if res_m else 0
            bw = int(bw_m.group(1)) if bw_m else 0
            variant = ''
            for j in range(i + 1, min(i + 4, len(lines))):
                v = lines[j].strip()
                if v and not v.startswith('#'):
                    variant = v if v.startswith('http') else f"{base_url}/{v}"
                    break
            if variant:
                label = f"{height}p" if height else f"{bw // 1000}kbps"
                qualities.append({'url': variant, 'label': label, 'height': height})
        qualities.sort(key=lambda x: x['height'], reverse=True)
        return qualities
    except Exception:
        return []


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.get_json()
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'success': False, 'error': 'URL არ არის მითითებული'})

    # გვერდის სკრეიპინგი
    page_info = scrape_page(url)

    if not page_info['success']:
        # სკრეიპინგი ვერ მოხდა — yt-dlp-ით სცადოს
        ytdlp_info = get_yt_dlp_info(url, referer='https://mykadri.tv/')
        return jsonify({
            'success': True,
            'title': ytdlp_info.get('title', 'უცნობი'),
            'poster': ytdlp_info.get('thumbnail', ''),
            'is_series': False,
            'episodes': [],
            'sources': [],
            'labels': [],
            'formats': ytdlp_info.get('formats', []),
            'duration': ytdlp_info.get('duration'),
            'url': url,
            'scrape_error': page_info.get('error', '')
        })

    # m3u8 სოურსებზე ხარისხის ვარიანტების ამოღება (yt-dlp-ის მაგივრად)
    sources_with_labels = []
    for i, src in enumerate(page_info.get('sources', [])):
        lang_lbl = page_info['labels'][i] if i < len(page_info.get('labels', [])) else ''
        if '.m3u8' in src:
            qualities = get_hls_qualities(src, referer=url)
            if qualities:
                for q in qualities:
                    lbl = f"{lang_lbl} {q['label']}".strip() if lang_lbl else q['label']
                    sources_with_labels.append({'url': q['url'], 'label': lbl})
            else:
                sources_with_labels.append({'url': src, 'label': lang_lbl or 'საუკეთესო'})
        else:
            sources_with_labels.append({'url': src, 'label': lang_lbl})

    result = {
        'success': True,
        'title': page_info.get('title', 'უცნობი'),
        'poster': page_info.get('poster', ''),
        'is_series': page_info.get('is_series', False),
        'episodes': page_info.get('episodes', []),
        'seasons': page_info.get('seasons', {}),
        'sources': page_info.get('sources', []),
        'sources_labeled': sources_with_labels,
        'labels': page_info.get('labels', []),
        'iframes': page_info.get('iframes', []),
        'formats': [],
        'episode_id': page_info.get('episode_id', ''),
        'url': url
    }

    return jsonify(result)


@app.route('/api/hls_qualities', methods=['POST'])
def hls_qualities_api():
    data = request.get_json()
    url = data.get('url', '')
    referer = data.get('referer', 'https://mykadri.tv/')
    if not url:
        return jsonify({'qualities': []})
    qualities = get_hls_qualities(url, referer=referer)
    return jsonify({'qualities': qualities})


@app.route('/api/jobs', methods=['GET'])
def get_all_jobs():
    jobs_data = load_jobs_file()
    # Merge with live in-memory status for active jobs
    for job_id, job in jobs_data.items():
        if job_id in downloads:
            d = downloads[job_id]
            job['status'] = d['status']
            job['progress'] = d['progress']
            job['speed'] = d.get('speed', '')
            job['eta'] = d.get('eta', '')
            job['filename'] = job.get('filename') or d.get('filename', '')
            job['error'] = d.get('error', '')
    return jsonify({'jobs': jobs_data})


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    jobs_data = load_jobs_file()
    jobs_data.pop(job_id, None)
    save_jobs_file(jobs_data)
    # Stop if still running
    if job_id in downloads:
        proc = downloads[job_id].get('process')
        if proc:
            try: proc.terminate()
            except Exception: pass
        downloads.pop(job_id, None)
    return jsonify({'success': True})


@app.route('/api/jobs', methods=['DELETE'])
def delete_all_jobs():
    jobs_data = load_jobs_file()
    for job_id in list(jobs_data.keys()):
        if job_id in downloads:
            proc = downloads[job_id].get('process')
            if proc:
                try: proc.terminate()
                except Exception: pass
            downloads.pop(job_id, None)
    save_jobs_file({})
    return jsonify({'success': True})


@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.get_json()
    url = data.get('url', '')
    title = data.get('title', 'video')
    format_id = data.get('format_id', 'bestvideo+bestaudio/best')
    episode_id = data.get('episode_id', '').strip()  # e.g. "S01E01"
    quality_height = int(data.get('quality_height', 0) or 0)  # e.g. 720
    subdir = data.get('subdir', '').strip()  # e.g. "Vikings/Season 01"
    resume_seconds = float(data.get('resume_seconds', 0) or 0)

    if not url:
        return jsonify({'success': False, 'error': 'No URL'})

    job_id = str(uuid.uuid4())[:8]
    downloads[job_id] = {
        'status': 'starting',
        'progress': 0,
        'speed': '',
        'eta': '',
        'filename': '',
        'paused': False,
        'process': None
    }

    # Persist job to disk
    jobs_data = load_jobs_file()
    jobs_data[job_id] = {
        'title': title,
        'url': url,
        'episode_id': episode_id,
        'subdir': subdir,
        'status': 'starting',
        'progress': 0,
        'filename': '',
        'error': '',
        'created_at': datetime.now().isoformat()
    }
    save_jobs_file(jobs_data)

    def run_download():
        nonlocal resume_seconds
        safe_title = re.sub(r'[^\w\s\-_ა-ჰ]', '', title).strip()[:60]
        safe_ep = re.sub(r'[^\w\-]', '', episode_id)
        filename_base = safe_ep if safe_ep else safe_title
        is_hls = '.m3u8' in url

        # Determine output directory
        if subdir:
            safe_sub = re.sub(r'[^\w\s\-_/ა-ჰ]', '', subdir).strip().strip('/')
            out_dir = os.path.join(DOWNLOAD_DIR, safe_sub) if safe_sub else DOWNLOAD_DIR
        else:
            out_dir = DOWNLOAD_DIR
        os.makedirs(out_dir, exist_ok=True)

        try:
            # Resolve specific quality variant for HLS master playlists
            actual_url = url
            if is_hls and quality_height > 0:
                qualities = get_hls_qualities(url, referer='https://mykadri.tv/')
                if qualities:
                    best = min(qualities, key=lambda q: abs(q['height'] - quality_height))
                    actual_url = best['url']

            if is_hls:
                # ffmpeg: direct HLS/m3u8 download with proper headers
                output_path = os.path.join(out_dir, f"{filename_base}.mp4")
                downloads[job_id]['filename'] = os.path.basename(output_path)

                # Get total duration for progress %
                total_duration_s = 0
                try:
                    probe = subprocess.run(
                        ['ffprobe', '-v', 'quiet',
                         '-user_agent', HEADERS['User-Agent'],
                         '-print_format', 'json', '-show_format', actual_url],
                        capture_output=True, text=True, timeout=20
                    )
                    probe_data = json.loads(probe.stdout)
                    total_duration_s = float(probe_data['format']['duration'])
                except Exception:
                    pass
                downloads[job_id]['total_duration_s'] = total_duration_s

                # Resume: write to temp file, then concat with existing partial file
                if resume_seconds > 0 and os.path.exists(output_path):
                    write_path = os.path.join(out_dir, f"{filename_base}_pt2.mp4")
                else:
                    write_path = output_path
                    resume_seconds = 0  # no partial file to concat with

                cmd = [
                    'ffmpeg', '-y',
                    '-user_agent', HEADERS['User-Agent'],
                    '-headers', 'Referer: https://mykadri.tv/\r\n',
                ]
                if resume_seconds > 0:
                    cmd += ['-ss', str(int(resume_seconds))]
                cmd += [
                    '-i', actual_url,
                    '-c', 'copy',
                    '-movflags', '+faststart',
                    '-progress', 'pipe:1',
                    '-nostats',
                    write_path
                ]
            else:
                # yt-dlp for direct mp4/other URLs — --continue handles partial resume
                output_path = os.path.join(out_dir, f"{filename_base}.%(ext)s")
                downloads[job_id]['filename'] = f"{filename_base}.mp4"
                cmd = [
                    YT_DLP,
                    '-f', format_id,
                    '--user-agent', HEADERS['User-Agent'],
                    '--referer', 'https://mykadri.tv/',
                    '--output', output_path,
                    '--newline', '--progress',
                    '--continue',
                    '--retries', '5',
                    '--fragment-retries', '10',
                    '--merge-output-format', 'mp4',
                    url
                ]

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            downloads[job_id]['process'] = proc
            downloads[job_id]['status'] = 'downloading'

            for line in proc.stdout:
                line = line.strip()
                if downloads[job_id].get('paused'):
                    proc.terminate()
                    downloads[job_id]['status'] = 'paused'
                    break

                if is_hls:
                    if line.startswith('out_time_ms='):
                        try:
                            out_s = int(line.split('=')[1]) / 1_000_000
                            if total_duration_s > 0:
                                pct = (resume_seconds + out_s) / total_duration_s * 100
                                downloads[job_id]['progress'] = min(pct, 99)
                        except ValueError:
                            pass
                    elif line.startswith('speed=') and line != 'speed=N/A':
                        downloads[job_id]['speed'] = line.split('=')[1]
                    elif line.startswith('total_size='):
                        try:
                            mb = int(line.split('=')[1]) / 1_048_576
                            downloads[job_id]['eta'] = f'{mb:.1f}MB'
                        except ValueError:
                            pass
                else:
                    prog_match = re.search(r'\[download\]\s+([\d.]+)%', line)
                    if prog_match:
                        downloads[job_id]['progress'] = float(prog_match.group(1))

                    speed_match = re.search(r'at\s+([\d.]+\s*\w+/s)', line)
                    if speed_match:
                        downloads[job_id]['speed'] = speed_match.group(1)

                    eta_match = re.search(r'ETA\s+(\S+)', line)
                    if eta_match:
                        downloads[job_id]['eta'] = eta_match.group(1)

                    dest_match = re.search(r'Destination:\s+(.+)', line)
                    if dest_match:
                        downloads[job_id]['filename'] = os.path.basename(dest_match.group(1))

                    if 'ERROR' in line:
                        downloads[job_id]['error'] = line

            proc.wait()
            if proc.returncode == 0 and not downloads[job_id].get('paused'):
                # HLS resume: concatenate part1 + part2
                if is_hls and resume_seconds > 0 and write_path != output_path:
                    if os.path.exists(output_path) and os.path.exists(write_path):
                        list_file = os.path.join(out_dir, f"{filename_base}_list.txt")
                        final_tmp = os.path.join(out_dir, f"{filename_base}_joined.mp4")
                        try:
                            with open(list_file, 'w') as lf:
                                lf.write(f"file '{output_path}'\n")
                                lf.write(f"file '{write_path}'\n")
                            subprocess.run(
                                ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                                 '-i', list_file, '-c', 'copy', final_tmp],
                                check=True, capture_output=True
                            )
                            os.replace(final_tmp, output_path)
                        finally:
                            if os.path.exists(write_path): os.remove(write_path)
                            if os.path.exists(list_file): os.remove(list_file)
                downloads[job_id]['status'] = 'done'
                downloads[job_id]['progress'] = 100
                update_job_status(job_id, 'done', 100,
                                  filename=downloads[job_id].get('filename', ''))
            elif not downloads[job_id].get('paused'):
                downloads[job_id]['status'] = 'error'
                update_job_status(job_id, 'error',
                                  error=downloads[job_id].get('error', ''))

        except Exception as e:
            downloads[job_id]['status'] = 'error'
            downloads[job_id]['error'] = str(e)
            update_job_status(job_id, 'error', error=str(e))

    t = threading.Thread(target=run_download, daemon=True)
    t.start()

    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/pause/<job_id>', methods=['POST'])
def pause_download(job_id):
    if job_id in downloads:
        downloads[job_id]['paused'] = True
        proc = downloads[job_id].get('process')
        if proc:
            proc.terminate()
        downloads[job_id]['status'] = 'paused'

        # Calculate how many seconds were already downloaded
        paused_at_seconds = 0
        total_s = downloads[job_id].get('total_duration_s', 0)
        progress = downloads[job_id].get('progress', 0)
        if total_s > 0:
            paused_at_seconds = total_s * progress / 100

        downloads[job_id]['paused_at_seconds'] = paused_at_seconds
        jobs_data = load_jobs_file()
        if job_id in jobs_data:
            jobs_data[job_id]['paused_at_seconds'] = paused_at_seconds
            save_jobs_file(jobs_data)

        return jsonify({'success': True, 'paused_at_seconds': paused_at_seconds})
    return jsonify({'success': False})


@app.route('/api/resume/<job_id>', methods=['POST'])
def resume_download(job_id):
    """Resume by restarting download with --continue flag"""
    if job_id not in downloads:
        return jsonify({'success': False})

    job = downloads[job_id]
    # Resume logic would need original URL stored - simplified here
    downloads[job_id]['paused'] = False
    downloads[job_id]['status'] = 'resumed'
    return jsonify({'success': True, 'message': 'Restart download to resume'})


@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    if job_id in downloads:
        d = downloads[job_id]
        return jsonify({
            'status': d['status'],
            'progress': d['progress'],
            'speed': d['speed'],
            'eta': d['eta'],
            'filename': d['filename'],
            'error': d.get('error', '')
        })
    return jsonify({'status': 'not_found'})


@app.route('/api/downloads')
def list_downloads():
    files = []
    if os.path.exists(DOWNLOAD_DIR):
        for f in os.listdir(DOWNLOAD_DIR):
            fpath = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(fpath):
                files.append({
                    'name': f,
                    'size': os.path.getsize(fpath),
                    'path': fpath
                })
    return jsonify({'files': files, 'dir': DOWNLOAD_DIR})


if __name__ == '__main__':
    print(f"\n{'='*50}")
    print(f"  KadriTV Download Manager")
    print(f"  გახსენი: http://localhost:5001")
    print(f"  გადმოსაწერი: {DOWNLOAD_DIR}")
    print(f"{'='*50}\n")
    app.run(debug=False, host='0.0.0.0', port=5001)
