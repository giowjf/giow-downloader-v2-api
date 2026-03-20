import os
import subprocess
import base64
import tempfile
import time
import hashlib
import yt_dlp
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}},
     allow_headers=["Content-Type"], methods=["GET", "POST", "OPTIONS"])

# ─── Cache ──────────────────────────────────────────────────────────────────
_cookie_cache = {"path": None, "loaded_at": 0}
COOKIE_TTL = 3600

_analyze_cache = {}
ANALYZE_TTL = 300  # URLs assinadas do YT duram ~6h, cache de 5min é seguro


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def cors_preflight():
    r = make_response()
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return r


# ─── Cookies ────────────────────────────────────────────────────────────────

def get_cookie_file():
    global _cookie_cache
    now = time.time()
    if (_cookie_cache["path"]
            and os.path.exists(_cookie_cache["path"])
            and now - _cookie_cache["loaded_at"] < COOKIE_TTL):
        return _cookie_cache["path"]
    path = _load_cookie_file()
    _cookie_cache = {"path": path, "loaded_at": now}
    return path


def _validate_cookie_data(data):
    """Verifica se o conteúdo parece um arquivo Netscape válido."""
    if not data or len(data.strip()) < 10:
        return False
    lines = [l for l in data.strip().splitlines() if l.strip()]
    if not lines:
        return False
    # Deve começar com cabeçalho Netscape ou ter linhas com formato de cookie
    has_header = any("Netscape" in l or l.startswith("#") for l in lines[:3])
    has_cookie = any(len(l.split("\t")) >= 6 for l in lines if not l.startswith("#"))
    return has_header or has_cookie


def _load_cookie_file():
    cookies_b64 = os.environ.get("YOUTUBE_COOKIES_B64")
    if cookies_b64:
        try:
            data = base64.b64decode(cookies_b64).decode("utf-8")
            if not _validate_cookie_data(data):
                print(f"[cookies] AVISO: B64 não parece formato Netscape válido")
            else:
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, dir="/tmp")
                tmp.write(data); tmp.flush(); tmp.close()
                print(f"[cookies] B64 ({len(data)} bytes)")
                return tmp.name
        except Exception as e:
            print(f"[cookies] Erro B64: {e}")

    if os.path.exists("/etc/secrets/cookies.txt"):
        try:
            with open("/etc/secrets/cookies.txt") as f:
                data = f.read()
            if not _validate_cookie_data(data):
                print(f"[cookies] AVISO: /etc/secrets não parece formato Netscape válido — ignorando")
                print(f"[cookies] Conteúdo (primeiros 100 chars): {repr(data[:100])}")
            else:
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, dir="/tmp")
                tmp.write(data); tmp.flush(); tmp.close()
                print(f"[cookies] /etc/secrets ({len(data)} bytes)")
                return tmp.name
        except Exception as e:
            print(f"[cookies] Erro secrets: {e}")

    print("[cookies] Nenhum cookie válido — usando clientes sem cookie")
    return None


# ─── Extração ───────────────────────────────────────────────────────────────
# Clientes que retornam URLs sem amarração de IP:
# android e ios usam URLs que o YouTube não vincula ao IP do extrator,
# pois celulares mudam de rede constantemente — o app oficial usa as mesmas URLs.
# Isso permite que o browser do usuário baixe diretamente do YouTube.

# Ordem de prioridade:
# android/ios retornam URLs sem &ip= — browser baixa direto do YouTube sem proxy
# mweb retorna URLs com &ip= vinculado ao servidor — requer proxy, muito mais lento
DIRECT_CLIENTS = [
    "android",   # DASH sem &ip= — download direto pelo browser
    "ios",       # HLS/DASH sem &ip= — download direto pelo browser
    "mweb",      # último recurso — URLs com &ip=, requer Worker como proxy
]


def extract_with_direct_urls(url):
    """
    Extrai metadados e URLs diretas dos streams.
    Usa clientes android/ios que retornam URLs sem amarração de IP,
    permitindo download direto pelo browser do usuário.
    """
    url_key = hashlib.md5(url.encode()).hexdigest()

    # Cache hit
    if url_key in _analyze_cache:
        cached, cached_at = _analyze_cache[url_key]
        if time.time() - cached_at < ANALYZE_TTL:
            print(f"[analyze] Cache hit — {url[:50]}")
            return cached
        del _analyze_cache[url_key]

    cookie_path = get_cookie_file()
    last_error = None

    for client in DIRECT_CLIENTS:
        print(f"[analyze] Tentando client={client}")
        try:
            opts = {
                "quiet": True,
                "skip_download": True,
                "nocheckcertificate": True,
                "check_formats": False,
                "ignore_no_formats_error": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": [client],
                        "formats": ["missing_pot"],
                    }
                },
                "http_headers": {"Accept-Language": "en-US,en;q=0.9"},
                "js_runtimes": {"node": {}},
            }

            # Passa cookies para todos os clientes — necessário em IPs de datacenter
            # android/ios suportam cookies desde yt-dlp 2024
            if cookie_path:
                opts["cookiefile"] = cookie_path

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                raise ValueError("retornou vazio")

            formats = info.get("formats") or []

            # Filtra formatos com URL direta válida e vídeo real
            video_formats = [
                f for f in formats
                if f.get("url")
                and (f.get("vcodec") or "none") != "none"
                and (f.get("height") or 0) > 0
                and not f.get("url", "").startswith("manifest")
            ]

            # Formatos só de áudio com URL direta
            audio_formats = [
                f for f in formats
                if f.get("url")
                and (f.get("acodec") or "none") != "none"
                and (f.get("vcodec") or "none") == "none"
                and not f.get("url", "").startswith("manifest")
            ]

            print(f"[analyze] client={client} — {len(video_formats)} vídeo, {len(audio_formats)} áudio com URL direta")

            if not video_formats and not audio_formats:
                last_error = f"client={client} sem URLs diretas utilizáveis"
                continue

            # Detecta se URLs têm &ip= (vinculadas ao IP do servidor)
            # android/ios não têm &ip= — browser baixa direto
            # mweb tem &ip= — precisa passar pelo Worker como proxy
            sample_url = (video_formats[0] if video_formats else audio_formats[0]).get("url", "")
            urls_have_ip = "&ip=" in sample_url
            print(f"[analyze] client={client} — URLs com &ip=: {urls_have_ip}")

            result = {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "client_used": client,
                "urls_need_proxy": urls_have_ip,  # front usa Worker só quando necessário
                "video_formats": video_formats,
                "audio_formats": audio_formats,
            }

            _analyze_cache[url_key] = (result, time.time())
            return result

        except Exception as e:
            last_error = str(e)
            print(f"[analyze] client={client} falhou: {last_error[:200]}")
            continue

    raise Exception(f"Todos os clientes falharam. Último: {last_error}")


def build_response_formats(video_formats, audio_formats):
    """
    Monta a lista de formatos para o front.
    Cada formato tem a URL direta do stream para download pelo browser.
    Para vídeos sem áudio (DASH), inclui também a URL do melhor áudio.
    """
    # Melhor áudio disponível
    best_audio = None
    if audio_formats:
        best_audio = max(
            audio_formats,
            key=lambda f: f.get("abr") or f.get("tbr") or 0
        )

    seen = set()
    result = []

    # Ordena por resolução decrescente
    sorted_video = sorted(
        video_formats,
        key=lambda f: (f.get("height") or 0),
        reverse=True
    )

    for f in sorted_video:
        height = f.get("height") or 0
        ext = f.get("ext") or "mp4"
        resolution = f.get("resolution") or f"{height}p"

        key = (ext, resolution)
        if key in seen:
            continue
        seen.add(key)

        has_audio = (f.get("acodec") or "none") != "none"

        entry = {
            "format_id": f.get("format_id"),
            "ext": ext,
            "resolution": resolution,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "fps": f.get("fps"),
            "video_url": f.get("url"),
            "has_audio": has_audio,
        }

        # Se não tem áudio embutido (DASH puro), inclui URL do áudio separado
        if not has_audio and best_audio:
            entry["audio_url"] = best_audio.get("url")
            entry["audio_ext"] = best_audio.get("ext", "m4a")

        result.append(entry)

    # MP3 — usa a URL do melhor áudio direto
    if best_audio:
        result.append({
            "format_id": "mp3-direct",
            "ext": "mp3",
            "resolution": "audio only",
            "filesize": best_audio.get("filesize") or best_audio.get("filesize_approx"),
            "fps": None,
            "video_url": best_audio.get("url"),
            "has_audio": True,
            "is_audio_only": True,
        })

    return result


# ─── Rotas ──────────────────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST", "OPTIONS"])
def analyze():
    if request.method == "OPTIONS":
        return cors_preflight()

    data = request.json
    url = data.get("url") if data else None
    if not url:
        return jsonify({"error": "missing url"}), 400

    print(f"[analyze] {url[:80]}")
    try:
        t0 = time.time()
        info = extract_with_direct_urls(url)
        elapsed = time.time() - t0

        formats = build_response_formats(
            info["video_formats"],
            info["audio_formats"]
        )

        print(f"[analyze] OK em {elapsed:.1f}s — {len(formats)} formatos — client={info['client_used']}")

        return jsonify({
            "title": info["title"],
            "duration": info["duration"],
            "thumbnail": info["thumbnail"],
            "uploader": info["uploader"],
            "client_used": info["client_used"],
            "urls_need_proxy": info.get("urls_need_proxy", True),
            "formats": formats,
            "elapsed": round(elapsed, 2),
        })

    except Exception as e:
        print(f"[analyze] ERRO: {e}")
        return jsonify({"error": "Falha ao extrair", "details": str(e)}), 500


@app.route("/")
def health():
    cookie_path = get_cookie_file()
    try:
        node = subprocess.run(["node", "--version"],
                              capture_output=True, text=True, timeout=5)
        node_ver = node.stdout.strip() if node.returncode == 0 else "unavailable"
    except Exception:
        node_ver = "unavailable"

    return jsonify({
        "status": "running",
        "version": "2.0-direct-url",
        "yt_dlp": yt_dlp.version.__version__,
        "node": node_ver,
        "cookies": cookie_path is not None,
        "architecture": "direct-url — browser downloads from YouTube directly",
    })


@app.route("/diag")
def diag():
    print("[diag] Iniciando...")

    # ── Diagnóstico de cookies ─────────────────────────────────────────────
    secret_exists = os.path.exists("/etc/secrets/cookies.txt")
    secret_size = 0
    secret_preview = ""
    secret_valid = False

    if secret_exists:
        try:
            with open("/etc/secrets/cookies.txt") as f:
                raw = f.read()
            secret_size = len(raw)
            secret_preview = repr(raw[:120])
            secret_valid = _validate_cookie_data(raw)
        except Exception as e:
            secret_preview = f"ERRO ao ler: {e}"

    cookie_path = get_cookie_file()

    cookie_info = {
        "secret_file_exists": secret_exists,
        "secret_file_size_bytes": secret_size,
        "secret_file_preview": secret_preview,
        "secret_file_valid_netscape": secret_valid,
        "cookie_path_loaded": cookie_path,
        "cookie_cache_active": cookie_path is not None,
    }

    print(f"[diag] Cookies: {cookie_info}")

    # ── Teste real com vídeo curto ────────────────────────────────────────
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    test_result = {"ok": False}
    try:
        info = extract_with_direct_urls(test_url)
        formats = build_response_formats(info["video_formats"], info["audio_formats"])
        video_with_url = [f for f in formats if f.get("video_url") and not f.get("is_audio_only")]
        test_result = {
            "ok": len(video_with_url) > 0,
            "total_formats": len(formats),
            "video_with_direct_url": len(video_with_url),
            "client_used": info["client_used"],
            "sample": [f["resolution"] for f in video_with_url[:4]],
        }
    except Exception as e:
        test_result = {"ok": False, "error": str(e)[:300]}

    overall = "OK" if test_result["ok"] else "PROBLEMAS"
    if not secret_valid:
        overall = "COOKIE_INVALIDO"

    return jsonify({
        "overall": overall,
        "cookies": cookie_info,
        "extraction": test_result,
    })


@app.route("/warmup")
def warmup():
    get_cookie_file()
    return jsonify({"status": "warm", "cache": len(_analyze_cache)})


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    global _analyze_cache, _cookie_cache
    n = len(_analyze_cache)
    _analyze_cache.clear()
    _cookie_cache = {"path": None, "loaded_at": 0}
    return jsonify({"cleared": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
