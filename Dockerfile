FROM python:3.11-slim

WORKDIR /app

# Node.js 20 para EJS nativo do yt-dlp
# Sem ffmpeg — não fazemos mais mux/conversão no servidor
RUN apt-get update && apt-get install -y curl \
  && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y nodejs \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", \
     "--timeout", "120", \
     "--workers", "2", \
     "--worker-class", "gevent", \
     "--worker-connections", "10", \
     "app:app"]
