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
USE_LOGO = True
LOGO_PATH = "/home/space/logo.png"
LOGO_POSITION = "top-left"
LOGO_SCALE = 0.1
MAKO_URL = "https://www.mako.co.il/culture-tv/articles/Article-c75a4149b6ef091027.htm"
PORT = int(os.environ.get("PORT", 8080))
SEGMENT_DURATION = 15     # shorter segments, less memory usage
SEGMENT_LIST_SIZE = 15    # fewer segments, less disk usage
RETRY_DELAY = 5
PLAYWRIGHT_TIMEOUT = 60000
TARGET_WIDTH = 640        # downscale video width for CPU/memory
TARGET_HEIGHT = 360       # downscale video height
VIDEO_BITRATE = "400k"    # reduce bitrate
AUDIO_BITRATE = "64k"     # reduce audio bitrate
# ----------------------------------------

def get_overlay_position(position):
    margin = 10
    return {
        "top-left": f"{margin}:{margin}",
        "top-right": f"main_w-overlay_w-{margin}:{margin}",
        "bottom-left": f"{margin}:main_h-overlay_h-{margin}",
        "bottom-right": f"main_w-overlay_w-{margin}:main_h-overlay_h-{margin}"
    }.get(position, f"{margin}:{margin}")

def cleanup():
    for f in os.listdir():
        if re.match(r'segment\d+\.ts', f):
            try:
                os.remove(f)
            except Exception:
                pass

# ---------------- HTTP SERVER ----------------
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
                while chunk := f.read(1024 * 1024):
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break

        except Exception as e:
            print(f"[ERROR] Failed to serve segment: {e}")

# ---------------- PLAYWRIGHT ----------------
def capture_m3u8_url(url):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
            page = browser.new_page()
            m3u8_url = None

            def handle_response(response):
                nonlocal m3u8_url
                if ".m3u8" in response.url:
                    m3u8_url = response.url

            page.on("response", handle_response)

            try:
                page.goto(url, timeout=PLAYWRIGHT_TIMEOUT)
                time.sleep(5)
            except Exception as e:
                print(f"[WARN] Page navigation failed: {e}")

            browser.close()
            return m3u8_url
    except Exception as e:
        print(f"[ERROR] Playwright failed: {e}")
        return None

# ---------------- STREAM WORKER (NON-BLOCKING) ----------------
def stream_worker(server):
    while True:
        m3u8_url = capture_m3u8_url(MAKO_URL)
        if not m3u8_url:
            print("[WARN] Could not get m3u8 URL, retrying...")
            time.sleep(RETRY_DELAY)
            continue

        cmd = [
            FFMPEG_PATH,
            "-i", m3u8_url,
            "-vf", f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}",
            "-c:v", "libx264",
            "-b:v", VIDEO_BITRATE,
            "-c:a", "aac",
            "-b:a", AUDIO_BITRATE,
            "-f", "segment",
            "-segment_time", str(SEGMENT_DURATION),
            "-segment_list", "playlist.m3u8",
            "-segment_list_size", str(SEGMENT_LIST_SIZE),
            "-reset_timestamps", "1"
        ]

        if USE_LOGO and os.path.exists(LOGO_PATH):
            overlay = get_overlay_position(LOGO_POSITION)
            cmd += ["-vf", f"movie={LOGO_PATH}[logo];[in][logo] overlay={overlay},scale={TARGET_WIDTH}:{TARGET_HEIGHT}"]

        cmd += [f"segment%03d.ts"]

        try:
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while process.poll() is None:
                with server.lock:
                    files = [f for f in os.listdir() if re.match(r'segment\d+\.ts', f)]
                    server.available_segments = set(sorted([int(re.search(r'(\d+)', f).group(1)) for f in files]))
                time.sleep(1)

        except Exception as e:
            print(f"[ERROR] FFmpeg process failed: {e}")
            time.sleep(RETRY_DELAY)

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
