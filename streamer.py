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
FFMPEG_PATH = "/usr/bin/ffmpeg"          # Linux ffmpeg path
USE_LOGO = True                           # Set to False to disable logo
LOGO_PATH = "/tmp/logo.png"               # Put your logo in /tmp or remove
LOGO_POSITION = "top-left"                # Options: top-left, top-right, bottom-left, bottom-right
LOGO_SCALE = 0.1                           # Logo width fraction
MAKO_URL = "https://www.mako.co.il/culture-tv/articles/Article-c75a4149b6ef091027.htm"
PORT = int(os.environ.get("PORT", 10000)) # Render port binding
SEGMENT_DURATION = 10                     # seconds per HLS segment
SEGMENT_LIST_SIZE = 30
MAX_RETRIES = 5
RETRY_DELAY = 5
PLAYWRIGHT_TIMEOUT = 60000

SEGMENT_DIR = "/tmp"
SEGMENT_FILE_TEMPLATE = os.path.join(SEGMENT_DIR, "segment%03d.ts")
SEGMENT_LIST_FILE = os.path.join(SEGMENT_DIR, "playlist.m3u8")
# ----------------------------------------

def get_overlay_position(position):
    margin = 10
    return {
        "top-left": f"{margin}:{margin}",
        "top-right": f"main_w-overlay_w-{margin}:{margin}",
        "bottom-left": f"{margin}:main_h-overlay_h-{margin}",
        "bottom-right": f"main_w-overlay_w-{margin}:main_h-overlay_h-{margin}"
    }.get(position, f"{margin}:{margin}")

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    pass

class HTTPStreamHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<h1>Service is running</h1><p>HLS at <a href='/stream.m3u8'>/stream.m3u8</a></p>")
        elif self.path == '/stream.m3u8':
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
                    f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION+1}",
                    f"#EXT-X-MEDIA-SEQUENCE:{segments[0]}"
                ]
                for seg in segments:
                    playlist.append(f"#EXTINF:{SEGMENT_DURATION:.3f},")
                    playlist.append(f"/segment{seg:03d}.ts")  # Leading slash!

            self.wfile.write("\n".join(playlist).encode())

        except Exception as e:
            print(f"[ERROR] Failed to serve playlist: {e}")

    def handle_segment(self):
        try:
            seg_num = int(re.search(r'segment(\d+).ts', self.path).group(1))
            file_path = os.path.join(SEGMENT_DIR, f"segment{seg_num:03d}.ts")
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
                while chunk := f.read(1024*1024):
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        print(f"[WARN] Client disconnected during segment {seg_num}")
                        break

        except Exception as e:
            print(f"[ERROR] Failed to serve segment: {e}")

# ---------------- Playwright + FFmpeg logic ----------------
def capture_m3u8_url():
    for attempt in range(MAX_RETRIES):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
                page = browser.new_page()
                page.goto(MAKO_URL, timeout=PLAYWRIGHT_TIMEOUT)
                # Example: extract video source from page
                video_url = page.evaluate("""() => {
                    const v = document.querySelector('video');
                    return v ? v.src : '';
                }""")
                browser.close()
                if video_url:
                    return video_url
        except Exception as e:
            print(f"[WARN] Failed to capture URL (attempt {attempt+1}): {e}")
            time.sleep(RETRY_DELAY)
    return None

def stream_worker(httpd):
    seg_index = 0
    while True:
        video_url = capture_m3u8_url()
        if not video_url:
            print("[ERROR] Could not get video URL, retrying in 5s")
            time.sleep(RETRY_DELAY)
            continue

        ffmpeg_cmd = [
            FFMPEG_PATH,
            "-i", video_url,
            "-c:v", "copy",
            "-c:a", "aac",
            "-f", "segment",
            "-segment_time", str(SEGMENT_DURATION),
            "-segment_list_size", str(SEGMENT_LIST_SIZE),
            "-segment_list", SEGMENT_LIST_FILE,
            SEGMENT_FILE_TEMPLATE
        ]

        if USE_LOGO and os.path.exists(LOGO_PATH):
            overlay = get_overlay_position(LOGO_POSITION)
            ffmpeg_cmd.insert(-2, "-vf")
            ffmpeg_cmd.insert(-2, f"movie={LOGO_PATH} [logo]; [in][logo] overlay={overlay} [out]")

        print(f"[INFO] Running FFmpeg: {' '.join(ffmpeg_cmd)}")
        try:
            proc = subprocess.Popen(ffmpeg_cmd)
            while proc.poll() is None:
                # Update available segments
                with httpd.lock:
                    files = [f for f in os.listdir(SEGMENT_DIR) if f.startswith("segment") and f.endswith(".ts")]
                    httpd.available_segments = set(int(re.search(r'segment(\d+).ts', f).group(1)) for f in files)
                time.sleep(1)
        except Exception as e:
            print(f"[ERROR] FFmpeg failed: {e}")
        time.sleep(2)

# ---------------- Cleanup ----------------
def cleanup():
    for f in os.listdir(SEGMENT_DIR):
        if f.startswith("segment") or f == os.path.basename(SEGMENT_LIST_FILE):
            try:
                os.remove(os.path.join(SEGMENT_DIR, f))
            except:
                pass

# ---------------- MAIN ----------------
def main():
    if not os.path.exists(FFMPEG_PATH):
        print(f"[ERROR] FFmpeg not found at {FFMPEG_PATH}")
        return

    cleanup()
    server_address = ('0.0.0.0', PORT)
    httpd = ThreadingHTTPServer(server_address, HTTPStreamHandler)
    httpd.available_segments = set()
    httpd.lock = Lock()

    Thread(target=stream_worker, args=(httpd,), daemon=True).start()
    print("[INFO] Stream worker started")

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
