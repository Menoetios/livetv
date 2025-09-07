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
PORT = int(os.environ.get("PORT", 8080))  # Render requires dynamic port binding
SEGMENT_DURATION = 30
SEGMENT_LIST_SIZE = 45
MAX_RETRIES = 5
RETRY_DELAY = 5
PLAYWRIGHT_TIMEOUT = 60000
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
        """Handle HEAD requests (Render health check)."""
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
                        print(f"[WARN] Client disconnected during segment {seg_num}")
                        break

        except Exception as e:
            print(f"[ERROR] Failed to serve segment: {e}")

# ---------------- (rest of your Playwright + ffmpeg logic unchanged) ----------------
# keep your capture_m3u8_url, get_highest_quality_url, stream_worker, cleanup

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
