import os
import subprocess
import time
from threading import Thread, Lock
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from playwright.sync_api import sync_playwright
import re

# ---------------- CONFIG ----------------
FFMPEG_PATH = "/usr/bin/ffmpeg"
USE_LOGO = False
LOGO_PATH = "/app/logo.png"
LOGO_POSITION = "top-left"
LOGO_SCALE = 0.1
MAKO_URL = "https://www.mako.co.il/culture-tv/articles/Article-c75a4149b6ef091027.htm"
PORT = int(os.environ.get("PORT", 8080))
SEGMENT_DURATION = 12
SEGMENT_LIST_SIZE = 12
MAX_RETRIES = 3
RETRY_DELAY = 10
PLAYWRIGHT_TIMEOUT = 30000
TMP_DIR = "/tmp"
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
        if self.path == '/' or self.path == '/health':
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
                    f"#EXT-X-TARGETDURATION:{SEGMENT_DURATION + 1}",
                    f"#EXT-X-MEDIA-SEQUENCE:{segments[0]}"
                ]
                for seg in segments:
                    playlist.append(f"#EXTINF:{SEGMENT_DURATION:.3f},")
                    playlist.append(f"segment{seg:03d}.ts")

            self.wfile.write("\n".join(playlist).encode())

        except Exception as e:
            print(f"[ERROR] Failed to serve playlist: {e}")

    def handle_segment(self):
        try:
            match = re.search(r'segment(\d+).ts', self.path)
            if not match:
                self.send_response(400)
                self.end_headers()
                return

            seg_num = int(match.group(1))
            file_path = os.path.join(TMP_DIR, f"segment{seg_num:03d}.ts")
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
                while chunk := f.read(1024 * 1024):
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        print(f"[WARN] Client disconnected during segment {seg_num}")
                        break

        except Exception as e:
            print(f"[ERROR] Failed to serve segment: {e}")

# ---------------- HELPERS ----------------
def cleanup():
    for f in os.listdir(TMP_DIR):
        if f.startswith("segment") or f.endswith(".m3u8"):
            try:
                os.remove(os.path.join(TMP_DIR, f))
            except FileNotFoundError:
                pass

def capture_m3u8_url(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        page = browser.new_page()
        m3u8_url = None

        def handle_response(response):
            nonlocal m3u8_url
            if ".m3u8" in response.url:
                m3u8_url = response.url

        page.on("response", handle_response)
        page.goto(url, timeout=PLAYWRIGHT_TIMEOUT)
        time.sleep(5)
        browser.close()
        return m3u8_url

def stream_worker(server):
    retry = 0
    while retry < MAX_RETRIES:
        try:
            m3u8_url = capture_m3u8_url(MAKO_URL)
            if not m3u8_url:
                raise Exception("M3U8 not found")

            print(f"[INFO] Streaming from {m3u8_url}")

            ffmpeg_cmd = [
                FFMPEG_PATH,
                "-i", m3u8_url,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "28",
                "-r", "15",
                "-c:a", "aac",
                "-b:a", "64k",
                "-ac", "1",
                "-f", "hls",
                "-hls_time", str(SEGMENT_DURATION),
                "-hls_list_size", str(SEGMENT_LIST_SIZE),
                "-hls_flags", "delete_segments+append_list+independent_segments",
                "-hls_segment_filename", os.path.join(TMP_DIR, "segment%03d.ts"),
                os.path.join(TMP_DIR, "playlist.m3u8")
            ]

            proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            while proc.poll() is None:
                with server.lock:
                    segs = [
                        int(re.search(r"segment(\d+).ts", f).group(1))
                        for f in os.listdir(TMP_DIR) if f.startswith("segment")
                    ]
                    server.available_segments = set(segs)
                time.sleep(1)

        except Exception as e:
            print(f"[ERROR] Stream worker failed: {e}")
            retry += 1
            time.sleep(RETRY_DELAY)
        finally:
            cleanup()

    print("[FATAL] Max retries reached, exiting stream worker.")

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
