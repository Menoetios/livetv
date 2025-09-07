FROM python:3.11-slim

# Install system dependencies (FFmpeg, Chromium deps, fonts, etc.)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    libglib2.0-0 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    libdrm2 \
    libdbus-1-3 \
    libgbm1 \
    libasound2 \
    libatk1.0-0 \
    libcups2 \
    libatk-bridge2.0-0 \
    libxkbcommon0 \
    fonts-liberation \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only)
RUN playwright install --with-deps chromium

# Copy app code
COPY streamer.py /app/streamer.py

# Expose server port
EXPOSE 8080

# Run the streamer
CMD ["python", "streamer.py"]
