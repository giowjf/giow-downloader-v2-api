import os
import json
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
# web+default com cookies: retorna DASH completo (4K, 1080p) sem precisar GVS PO Token
# android/ios: retornam só 360p muxado em IPs de datacenter sem GVS PO Token
# mweb: fallback, retorna 360p muxado com &ip= vinculado ao servidor
DIRECT_CLIENTS = [
    "web",       # DASH completo com cookies — sem GVS PO Token necessário
    "android",   # fallback — só 360p sem GVS token em datacenter
    "mweb",      # último recurso — 360p com &ip=, requer Worker proxy
]


def _run_ytdlp_cli(url, client, cookie_path):
    """
    Chama yt-dlp via CLI subprocess com --dump-json.

    Vantagem sobre a biblioteca Python:
    - O processo Node.js/EJS persiste entre chamadas via socket
    - EJS fica "aquecido" — challenge resolve em ~1s na 2ª chamada em diante
    - Elimina os 8-15s de boot do Node.js a cada request

    Retorna o dict de info ou lança exceção.
    """
    cmd = [
        "yt-dlp",
        "--dump-single-json",   # retorna JSON único com campo "formats" completo
        "--no-check-certificate",
        "--ignore-no-formats-error",
        "--no-playlist",
        "--extractor-args", f"youtube:player_client={client}+formats=missing_pot",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
        "--js-runtimes", "node",
        "--format", "bestvideo+bestaudio/best",  # força yt-dlp a buscar todos os streams
        "--skip-download",
    ]

    # web/mweb aceitam cookies — android não aceita no yt-dlp atual
    if cookie_path and client != "android":
        cmd += ["--cookies", cookie_path]

    cmd.append(url)

    print(f"[analyze] CLI: yt-dlp client={client}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise ValueError(f"yt-dlp CLI erro: {result.stderr[:300]}")

    # --dump-json retorna uma linha JSON por vídeo
    lines = [l for l in result.stdout.strip().splitlines() if l.startswith("{")]
    if not lines:
        raise ValueError("yt-dlp CLI retornou saída vazia")

    return json.loads(lines[-1])


def extract_with_direct_urls(url):
    """
    Extrai metadados e URLs diretas via CLI subprocess.
    O CLI mantém o Node.js/EJS aquecido entre chamadas — muito mais rápido
    que a biblioteca Python que reinicia o Node.js a cada chamada.
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
        try:
            info = _run_ytdlp_cli(url, client, cookie_path)

            if not info:
                raise ValueError("retornou vazio")

            formats = info.get("formats") or []

            # Filtra formatos com URL direta válida e vídeo real
            def is_direct_url(f):
                """Retorna True apenas para URLs diretas de arquivo — não manifests/HLS."""
                url = f.get("url") or ""
                protocol = f.get("protocol") or ""
                ext = f.get("ext") or ""
                # Exclui: m3u8, manifests, dash XML, mhtml (thumbnails)
                if ext in ("m3u8", "mhtml"):
                    return False
                if protocol in ("m3u8", "m3u8_native", "dash", "http_dash_segments"):
                    return False
                if "m3u8" in url or ".m3u8" in url:
                    return False
                if url.startswith("manifest"):
                    return False
                # Só aceita http/https direto
                return url.startswith("http") and bool(url)

            video_formats = [
                f for f in formats
                if is_direct_url(f)
                and (f.get("vcodec") or "none") != "none"
                and (f.get("height") or 0) > 0
            ]

            # Formatos só de áudio com URL direta
            audio_formats = [
                f for f in formats
                if is_direct_url(f)
                and (f.get("acodec") or "none") != "none"
                and (f.get("vcodec") or "none") == "none"
            ]

            all_fmts = info.get("formats") or []
            print(f"[analyze] client={client} — total={len(all_fmts)} formatos, {len(video_formats)} vídeo, {len(audio_formats)} áudio")

            if not video_formats and not audio_formats:
                last_error = f"client={client} sem URLs diretas utilizáveis"
                continue

            # Detecta se URLs têm &ip= (vinculadas ao IP do servidor)
            # android/ios não têm &ip= — browser baixa direto sem proxy
            # mweb tem &ip= — precisa do Worker como proxy
            sample_url = (video_formats[0] if video_formats else audio_formats[0]).get("url", "")
            urls_have_ip = "&ip=" in sample_url
            print(f"[analyze] client={client} OK — urls_need_proxy={urls_have_ip}")

            result = {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "client_used": client,
                "urls_need_proxy": urls_have_ip,
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


@app.route("/debug-formats")
def debug_formats():
    """
    Diagnóstico completo — testa cada cliente individualmente e reporta:
    - Quantos formatos cada cliente retorna
    - Se URLs têm &ip= (vinculadas ao servidor) ou não
    - Quais resoluções estão disponíveis
    - Tempo de cada extração
    - Status dos cookies e Node.js
    """
    TEST_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    cookie_path = get_cookie_file()
    results = {}
    t_total = time.time()

    # ── 1. Ambiente ──────────────────────────────────────────────────────────
    def safe_cmd(cmd, timeout=30):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip() if r.returncode == 0 else f"ERRO: {r.stderr[:100]}"
        except Exception as e:
            return f"ERRO: {str(e)[:100]}"

    results["environment"] = {
        "node": safe_cmd(["node", "--version"]),
        "yt_dlp": safe_cmd(["yt-dlp", "--version"]),
        "ffmpeg": "ok" if "ERRO" not in safe_cmd(["ffmpeg", "-version"]) else "não encontrado",
        "cookies_valid": cookie_path is not None,
        "cookies_path": cookie_path,
    }

    # ── 2. Teste por cliente ──────────────────────────────────────────────────
    results["clients"] = {}

    for client in DIRECT_CLIENTS:
        t0 = time.time()
        try:
            cmd = [
                "yt-dlp",
                "--dump-single-json",
                "--no-check-certificate",
                "--ignore-no-formats-error",
                "--no-playlist",
                "--extractor-args", f"youtube:player_client={client}+formats=missing_pot",
                "--js-runtimes", "node",
                "--skip-download",
            ]
            # web precisa de cookies para DASH completo
            # android não aceita cookies no yt-dlp atual
            if cookie_path and client != "android":
                cmd += ["--cookies", cookie_path]
            cmd.append(TEST_URL)

            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            elapsed = round(time.time() - t0, 2)

            if r.returncode != 0:
                results["clients"][client] = {
                    "ok": False,
                    "elapsed": elapsed,
                    "error": r.stderr[-400:],
                }
                continue

            lines = [l for l in r.stdout.strip().splitlines() if l.startswith("{")]
            if not lines:
                results["clients"][client] = {
                    "ok": False,
                    "elapsed": elapsed,
                    "error": "sem JSON no output",
                    "stdout_preview": r.stdout[:200],
                }
                continue

            info = json.loads(lines[-1])
            fmts = info.get("formats") or []

            video_fmts = [f for f in fmts
                          if (f.get("vcodec") or "none") != "none"
                          and (f.get("height") or 0) > 0
                          and f.get("url")
                          and not f.get("url","").startswith("manifest")]

            audio_fmts = [f for f in fmts
                          if (f.get("acodec") or "none") != "none"
                          and (f.get("vcodec") or "none") == "none"
                          and f.get("url")]

            sample_url = (video_fmts[0] if video_fmts else (audio_fmts[0] if audio_fmts else {})).get("url","")
            urls_have_ip = "&ip=" in sample_url

            results["clients"][client] = {
                "ok": len(video_fmts) > 0,
                "elapsed_seconds": elapsed,
                "total_formats_raw": len(fmts),
                "video_formats_usable": len(video_fmts),
                "audio_formats_usable": len(audio_fmts),
                "urls_have_ip": urls_have_ip,
                "download_mode": "direto_youtube" if not urls_have_ip else "via_worker_proxy",
                "resolutions": sorted(
                    set(f"{f.get('height')}p" for f in video_fmts if f.get("height")),
                    key=lambda x: int(x[:-1]), reverse=True
                )[:8],
                "formats_detail": [
                    {
                        "id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "resolution": f"{f.get('height')}p" if f.get("height") else f.get("resolution"),
                        "vcodec": (f.get("vcodec") or "none")[:20],
                        "acodec": (f.get("acodec") or "none")[:20],
                        "filesize_mb": round(f.get("filesize",0)/1_048_576, 1) if f.get("filesize") else None,
                        "has_url": bool(f.get("url")),
                        "url_has_ip": "&ip=" in (f.get("url") or ""),
                    }
                    for f in fmts if f.get("url")
                ],
                "stderr_warnings": [l for l in r.stderr.splitlines() if "WARNING" in l][:5],
            }

        except subprocess.TimeoutExpired:
            results["clients"][client] = {"ok": False, "elapsed_seconds": 90, "error": "timeout (90s)"}
        except Exception as e:
            results["clients"][client] = {"ok": False, "error": str(e)[:300]}

    # ── 3. Sumário ────────────────────────────────────────────────────────────
    best_client = None
    best_direct = None
    for c, r in results["clients"].items():
        if r.get("ok"):
            if best_client is None:
                best_client = c
            if not r.get("urls_have_ip") and best_direct is None:
                best_direct = c

    results["summary"] = {
        "total_elapsed_seconds": round(time.time() - t_total, 2),
        "best_client_any": best_client,
        "best_client_direct_url": best_direct,
        "recommendation": (
            f"Usar '{best_direct}' — URLs sem &ip=, download direto do YouTube (rápido)"
            if best_direct else
            f"Usar '{best_client}' — URLs com &ip=, precisa Worker como proxy (lento)"
            if best_client else
            "Nenhum cliente funcionou — verificar cookies e Node.js"
        ),
    }

    return jsonify(results)


@app.route("/warmup")
def warmup():
    """Aquece o EJS rodando yt-dlp — chamadas seguintes ficam em 3-5s."""
    t0 = time.time()
    warmed = False
    try:
        cookie_path = get_cookie_file()
        cmd = [
            "yt-dlp", "--dump-single-json", "--skip-download",
            "--no-playlist", "--js-runtimes", "node",
            "--extractor-args", "youtube:player_client=web+formats=missing_pot",
            "--quiet",
        ]
        if cookie_path:
            cmd += ["--cookies", cookie_path]
        cmd.append("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        subprocess.run(cmd, capture_output=True, text=True, timeout=50)
        warmed = True
        print(f"[warmup] EJS aquecido em {round(time.time()-t0,2)}s")
    except Exception as e:
        print(f"[warmup] erro: {e}")
    return jsonify({
        "status": "warm",
        "ejs_warmed": warmed,
        "elapsed_seconds": round(time.time() - t0, 2),
        "cache_entries": len(_analyze_cache),
    })


@app.route("/proxy")
def proxy():
    """
    Proxy de download — faz fetch da URL do YouTube server-side e faz pipe ao browser.
    Substitui o Cloudflare Worker: sem throttling, banda total do Render.
    Suporta Range requests para retomada de download.
    """
    target_url = request.args.get("url")
    filename = request.args.get("filename", "video.mp4")

    if not target_url:
        return jsonify({"error": "missing url"}), 400

    # Valida que é URL do YouTube
    try:
        from urllib.parse import urlparse
        host = urlparse(target_url).hostname or ""
        if not any(host.endswith(h) for h in ["googlevideo.com", "youtube.com"]):
            return jsonify({"error": "url not allowed"}), 403
    except Exception:
        return jsonify({"error": "invalid url"}), 400

    # Repassa Range header se presente (suporte a retomada)
    fetch_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36",
        "Referer": "https://www.youtube.com/",
    }
    range_header = request.headers.get("Range")
    if range_header:
        fetch_headers["Range"] = range_header

    try:
        import urllib.request as urlreq
        req = urlreq.Request(target_url, headers=fetch_headers)
        yt_response = urlreq.urlopen(req, timeout=30)
    except Exception as e:
        return jsonify({"error": f"fetch failed: {str(e)[:200]}"}), 502

    # Streaming: lê em chunks e envia ao browser sem bufferizar tudo
    content_type = yt_response.headers.get("Content-Type", "video/mp4")
    content_length = yt_response.headers.get("Content-Length")
    status = yt_response.status

    safe_filename = "".join(c if c not in '"/\\:' else "_" for c in filename)[:150]

    def generate():
        try:
            while True:
                chunk = yt_response.read(256 * 1024)  # 256KB por chunk
                if not chunk:
                    break
                yield chunk
        finally:
            yt_response.close()

    headers = {
        "Content-Type": content_type,
        "Content-Disposition": f'attachment; filename="{safe_filename}"',
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range",
        "Cache-Control": "no-cache",
    }
    if content_length:
        headers["Content-Length"] = content_length

    return app.response_class(
        generate(),
        status=status,
        headers=headers,
        direct_passthrough=True,
    )


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    global _analyze_cache, _cookie_cache
    n = len(_analyze_cache)
    _analyze_cache.clear()
    _cookie_cache = {"path": None, "loaded_at": 0}
    return jsonify({"cleared": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
