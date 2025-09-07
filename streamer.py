import os
import subprocess
import time
from threading import Thread, Lock
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from playwright.sync_api import sync_playwright
import re
import random
import requests
from urllib.parse import urljoin

# ---------------- CONFIG ----------------
FFMPEG_PATH = "/usr/bin/ffmpeg"           # Linux ffmpeg path
USE_LOGO = True                           # Set to False to disable logo
LOGO_PATH = "/home/space/logo.png"        # Absolute path to logo
LOGO_POSITION = "top-left"                # Options: top-left, top-right, bottom-left, bottom-right
LOGO_SCALE = 0.1                          # Logo width as fraction of video width
MAKO_URL = "https://www.mako.co.il/culture-tv/articles/Article-c75a4149b6ef091027.htm"
PORT = 8080
SEGMENT_DURATION = 30
SEGMENT_LIST_SIZE = 45
MAX_RETRIES = 5
RETRY_DELAY = 5
PLAYWRIGHT_TIMEOUT = 60000
# ----------------------------------------

def get_overlay_position(position):
    margin = 10
    if position == "top-left":
        return f"{margin}:{margin}"
    elif position == "top-right":
        return f"main_w-overlay_w-{margin}:{margin}"
    elif position == "bottom-left":
        return f"{margin}:main_h-overlay_h-{margin}"
    elif position == "bottom-right":
        return f"main_w-overlay_w-{margin}:main_h-overlay_h-{margin}"
    return f"{margin}:{margin}"

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    pass

class HTTPStreamHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == '/stream.m3u8':
            self.handle_hls_playlist()
        elif self.path.startswith('/segment'):
            self.handle_segment()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_hls_playlist(self):
        try:
            self.send_response(200)
            self.send_header('Content-type', 'application/vnd.apple.mpegurl')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()

            with self.server.lock:
                segments = sorted(self.server.available_segments)
                if not segments:
                    self.wfile.write(b"#EXTM3U\n#EXT-X-VERSION:3\n")
                    return

                playlist = [
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION + 1}",
                    f"#EXT-X-MEDIA-SEQUENCE:{segments[0]}"
                ]
                for seg in segments:
                    playlist.append(f"#EXTINF:{SEGMENT_DURATION:.3f},")
                    playlist.append(f"segment{seg:03d}.ts")  # padded numbering

            try:
                self.wfile.write("\n".join(playlist).encode())
            except (BrokenPipeError, ConnectionResetError):
                print("[WARN] Client disconnected while sending playlist")

        except Exception as e:
            print(f"[ERROR] Failed to serve playlist: {e}")

    def handle_segment(self):
        try:
            seg_num = int(re.search(r'segment(\d+).ts', self.path).group(1))
            file_path = f"segment{seg_num:03d}.ts"
            if not os.path.exists(file_path):
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header('Content-type', 'video/MP2T')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', os.path.getsize(file_path))
            self.end_headers()

            with open(file_path, 'rb') as f:
                try:
                    while True:
                        data = f.read(1024*1024)
                        if not data:
                            break
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    print(f"[WARN] Client disconnected during segment {seg_num}")

        except Exception as e:
            print(f"[ERROR] Failed to serve segment: {e}")

# ---------------- M3U8 CAPTURE ----------------
def capture_m3u8_url():
    for attempt in range(MAX_RETRIES):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled',
                          '--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
                          f'--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                          f'(KHTML, like Gecko) Chrome/{random.randint(90,110)}.0.'
                          f'{random.randint(1000,9999)}.{random.randint(100,999)} Safari/537.36']
                )
                context = browser.new_context(viewport={"width":1920,"height":1080}, locale="en-US")
                page = context.new_page()

                def route_handler(route):
                    if any(ext in route.request.url.lower() for ext in ['.jpg','.png','.gif','.css','.woff','.ico']):
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/*", route_handler)

                m3u8_url = None
                def handle_request(request):
                    nonlocal m3u8_url
                    url = request.url.lower()
                    if ".m3u8" in url and any(x in url for x in ["index.m3u8","chunklist","manifest"]):
                        if not m3u8_url or "chunklist" in url:
                            m3u8_url = request.url
                            print(f"[DEBUG] Found M3U8 URL: {m3u8_url}")

                page.on("request", handle_request)
                print(f"[INFO] Loading page (Attempt {attempt+1}/{MAX_RETRIES})")
                page.goto(MAKO_URL, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT)

                try:
                    page.mouse.wheel(0, random.randint(100,300))
                    page.click("button[aria-label='Play'], button[title='Play'], .play-button", timeout=5000)
                except:
                    pass

                time.sleep(5)
                browser.close()
                if m3u8_url:
                    print(f"[SUCCESS] Captured M3U8 URL: {m3u8_url}")
                    return m3u8_url
        except Exception as e:
            print(f"[WARN] Attempt {attempt+1} failed: {str(e)}")
        time.sleep(RETRY_DELAY)
    print("[ERROR] Failed to capture M3U8 URL after all attempts")
    return None

def get_highest_quality_url(m3u8_url):
    try:
        headers = {"User-Agent":"Mozilla/5.0","Referer":MAKO_URL,"Accept":"*/*"}
        resp = requests.get(m3u8_url, headers=headers, timeout=10)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        variants = [line for line in lines if line.endswith(".m3u8") and not line.startswith("#")]
        if variants:
            highest = variants[-1]
            return highest if highest.startswith("http") else urljoin(m3u8_url.rsplit("/",1)[0]+"/", highest)
        return m3u8_url
    except Exception as e:
        print(f"[ERROR] Failed to parse M3U8: {e}")
        return m3u8_url

# ---------------- DOUBLE BUFFER STREAM WORKER ----------------
def stream_worker(server):
    last_url = None
    while True:
        try:
            next_url = capture_m3u8_url() or last_url
            if not next_url:
                time.sleep(RETRY_DELAY)
                continue
            next_url = get_highest_quality_url(next_url)
            last_url = next_url

            overlay_pos = get_overlay_position(LOGO_POSITION)

            # Base FFmpeg command (no logo)
            ffmpeg_cmd = [
                FFMPEG_PATH,
                "-fflags","+genpts+igndts+discardcorrupt",
                "-flags","+low_delay",
                "-probesize","50M",
                "-analyzeduration","100M",
                "-avioflags","direct",
                "-i", next_url,
                "-c:v","libx264",
                "-preset","veryfast",
                "-crf","23",
                "-r","25",
                "-c:a","aac",
                "-b:a","128k",
                "-f","hls",
                "-hls_time", str(SEGMENT_DURATION),
                "-hls_list_size", str(SEGMENT_LIST_SIZE),
                "-hls_flags","delete_segments+append_list+independent_segments",
                "-hls_segment_filename", "segment%03d.ts",
                "playlist.m3u8"
            ]

            # Add logo overlay if enabled
            if USE_LOGO and os.path.exists(LOGO_PATH):
                ffmpeg_cmd = [
                    FFMPEG_PATH,
                    "-fflags","+genpts+igndts+discardcorrupt",
                    "-flags","+low_delay",
                    "-probesize","50M",
                    "-analyzeduration","100M",
                    "-avioflags","direct",
                    "-i", next_url,
                    "-i", LOGO_PATH,
                    "-filter_complex", f"[1]scale=w=iw*{LOGO_SCALE}:h=-1[logo];[0][logo]overlay={overlay_pos}:format=auto",
                    "-c:v","libx264",
                    "-preset","veryfast",
                    "-crf","23",
                    "-r","25",
                    "-c:a","aac",
                    "-b:a","128k",
                    "-f","hls",
                    "-hls_time", str(SEGMENT_DURATION),
                    "-hls_list_size", str(SEGMENT_LIST_SIZE),
                    "-hls_flags","delete_segments+append_list+independent_segments",
                    "-hls_segment_filename", "segment%03d.ts",
                    "playlist.m3u8"
                ]

            ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE, universal_newlines=True)
            while True:
                line = ffmpeg_proc.stderr.readline()
                if not line:
                    break
                line = line.strip()
                print(line)

                if match := re.search(r"Opening 'segment(\d+)\.ts'", line):
                    seg_num = int(match.group(1))
                    with server.lock:
                        server.available_segments.add(seg_num)
                        # delete older segments safely
                        for old in list(server.available_segments):
                            if old < seg_num - SEGMENT_LIST_SIZE - 2:
                                server.available_segments.remove(old)
                                try: os.remove(f"segment{old:03d}.ts")
                                except: pass

                if any(err in line for err in ["403 Forbidden","404 Not Found","Failed to open segment","Connection timed out"]):
                    print("[WARN] Token expired or error detected, restarting FFmpeg...")
                    ffmpeg_proc.terminate()
                    time.sleep(1)
                    break
        except Exception as e:
            print(f"[ERROR] Stream worker error: {e}")
            time.sleep(RETRY_DELAY)

# ---------------- CLEANUP ----------------
def cleanup():
    for f in os.listdir('.'):
        if f.startswith("segment") and f.endswith(".ts"):
            try: os.remove(f)
            except: pass
    try:
        os.remove("playlist.m3u8")
    except: pass

# ---------------- MAIN ----------------
def main():
    if not os.path.exists(FFMPEG_PATH):
        print(f"[ERROR] FFmpeg not found at {FFMPEG_PATH}")
        return

    cleanup()
    server_address = ('', PORT)
    httpd = ThreadingHTTPServer(server_address, HTTPStreamHandler)
    httpd.available_segments = set()
    httpd.lock = Lock()

    Thread(target=stream_worker, args=(httpd,), daemon=True).start()

    print(f"[INFO] HTTP server running on port {PORT}")
    print(f"[INFO] Stream URL: http://localhost:{PORT}/stream.m3u8")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...")
    finally:
        httpd.server_close()
        cleanup()

if __name__ == "__main__":
    main()
