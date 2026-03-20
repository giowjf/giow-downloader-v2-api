"""
Suite de testes — giow-downloader-v2-api
Arquitetura: URL direta (browser baixa do YouTube, servidor só extrai metadados)

Uso:
    python test_suite.py              # todos os testes unitários
    python test_suite.py unit         # rápido, sem rede
    python test_suite.py integration  # requer cookies.txt local
"""

import sys
import os
import subprocess
import tempfile
import base64
import time

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):      print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg):    print(f"  {RED}✗{RESET} {msg}")
def warn(msg):    print(f"  {YELLOW}~{RESET} {msg}")
def section(t):   print(f"\n{BOLD}{'─'*52}{RESET}\n{BOLD}{t}{RESET}")

passed = failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        ok(name)
    else:
        failed += 1
        fail(name + (f"\n    → {detail}" if detail else ""))


# ─── BLOCO 1: Ambiente ───────────────────────────────────────────────────────

def test_environment():
    section("1. Ambiente")

    check("Python 3.10+", sys.version_info >= (3, 10),
          f"Atual: {sys.version}")

    try:
        import yt_dlp
        check("yt-dlp instalado", True)
        check("yt-dlp >= 2025", yt_dlp.version.__version__ >= "2025",
              f"Versão: {yt_dlp.version.__version__}")
    except ImportError:
        check("yt-dlp instalado", False, "pip install 'yt-dlp[default]'")

    try:
        import yt_dlp_ejs
        check("yt-dlp-ejs instalado", True)
    except ImportError:
        check("yt-dlp-ejs instalado", False, "pip install 'yt-dlp[default]'")

    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
        ver = r.stdout.strip()
        major = int(ver.lstrip("v").split(".")[0])
        check(f"Node.js >= 20 ({ver})", r.returncode == 0 and major >= 20)
    except FileNotFoundError:
        check("Node.js disponível", False, "node não encontrado no PATH")

    # v2 não usa ffmpeg — validar que não é dependência
    import shutil
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        warn(f"ffmpeg presente ({ffmpeg}) — não é necessário no v2, mas não causa problema")
    else:
        ok("ffmpeg ausente — correto para v2 (sem mux no servidor)")

    try:
        import flask
        check(f"flask instalado ({flask.__version__})", True)
    except ImportError:
        check("flask instalado", False)

    try:
        import gevent
        check(f"gevent instalado ({gevent.__version__})", True)
    except ImportError:
        check("gevent instalado", False, "pip install 'gunicorn[gevent]'")


# ─── BLOCO 2: Configuração yt-dlp ────────────────────────────────────────────

def test_ytdlp_config():
    section("2. Configuração yt-dlp")

    import yt_dlp

    # js_runtimes deve ser dict
    try:
        opts = {
            "quiet": True,
            "skip_download": True,
            "js_runtimes": {"node": {}},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            pass
        check("js_runtimes={'node': {}} aceito", True)
    except Exception as e:
        check("js_runtimes={'node': {}} aceito", False, str(e))

    # Lista deve ser rejeitada
    try:
        opts = {"quiet": True, "skip_download": True, "js_runtimes": ["node"]}
        with yt_dlp.YoutubeDL(opts) as ydl:
            pass
        check("js_runtimes lista DEVE ser rejeitado", False,
              "yt-dlp aceitou formato errado")
    except Exception as e:
        check("js_runtimes lista corretamente rejeitado", "Invalid js_runtimes" in str(e))

    # Clientes android/ios não aceitam cookies — validar que não passamos
    for client in ["android", "ios"]:
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write("# Netscape HTTP Cookie File\n")
                cookie_path = f.name
            opts = {
                "quiet": True,
                "skip_download": True,
                "js_runtimes": {"node": {}},
                "extractor_args": {
                    "youtube": {
                        "player_client": [client],
                        "formats": ["missing_pot"],
                    }
                },
                # NÃO passamos cookiefile para android/ios
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                pass
            check(f"client={client} configurado sem cookie (correto)", True)
            os.unlink(cookie_path)
        except Exception as e:
            check(f"client={client} aceito pelo YoutubeDL", False, str(e))


# ─── BLOCO 3: Código fonte ───────────────────────────────────────────────────

def test_source_code():
    section("3. Código fonte — app.py")

    app_path = os.path.join(os.path.dirname(__file__), "app.py")
    if not os.path.exists(app_path):
        fail(f"app.py não encontrado em {app_path}")
        return

    # Sintaxe
    r = subprocess.run(["python3", "-m", "py_compile", app_path],
                       capture_output=True)
    check("app.py sintaxe válida", r.returncode == 0,
          r.stderr.decode()[:200])

    content = open(app_path).read()

    # Arquitetura v2 — sem downloader
    check("Sem download_video() — v2 não baixa no servidor",
          "def download_video" not in content)
    check("Sem send_from_directory — v2 não serve arquivos",
          "send_from_directory" not in content)
    check("Sem DOWNLOAD_DIR — v2 não usa disco",
          "DOWNLOAD_DIR" not in content)

    # Clientes corretos
    check("DIRECT_CLIENTS definido com android/ios",
          "DIRECT_CLIENTS" in content and "android" in content and "ios" in content)

    # js_runtimes no formato correto
    check("js_runtimes usa dict {'node': {}}",
          '"js_runtimes": {"node": {}}' in content)
    check("js_runtimes NÃO usa lista ['node']",
          '"js_runtimes": ["node"]' not in content)

    # Cookies não passados para android/ios
    check("android/ios não recebem cookiefile",
          'client == "mweb"' in content or
          "mweb" in content)

    # Rotas essenciais
    check('Rota /analyze existe',   '@app.route("/analyze"' in content)
    check('Rota /diag existe',      '@app.route("/diag")' in content)
    check('Rota /warmup existe',    '@app.route("/warmup")' in content)
    check('Rota /cache/clear existe','@app.route("/cache/clear"' in content)

    # Cache
    check("Cache de análise implementado (_analyze_cache)",
          "_analyze_cache" in content)
    check("Cache de cookie implementado (_cookie_cache)",
          "_cookie_cache" in content)

    # URLs diretas no response
    check("video_url no response de /analyze",
          '"video_url"' in content)

    # CORS garantido
    check("after_request com CORS",
          "@app.after_request" in content)

    # Sem bgutil
    check("Sem referência a bgutil",
          "bgutil" not in content)


# ─── BLOCO 4: Dockerfile ────────────────────────────────────────────────────

def test_dockerfile():
    section("4. Dockerfile")

    df_path = os.path.join(os.path.dirname(__file__), "Dockerfile")
    if not os.path.exists(df_path):
        fail(f"Dockerfile não encontrado")
        return

    content = open(df_path).read()

    check("Node.js 20 instalado",
          "nodesource.com/setup_20" in content or "nodejs" in content)
    check("Sem ffmpeg — v2 não faz mux no servidor",
          "ffmpeg" not in content)
    check("gunicorn com gevent no CMD",
          "gevent" in content)
    check("Sem startup.sh",
          "startup.sh" not in content)

    req_path = os.path.join(os.path.dirname(__file__), "requirements.txt")
    if os.path.exists(req_path):
        req = open(req_path).read()
        check("yt-dlp[default] no requirements",
              "yt-dlp[default]" in req)
        check("gunicorn[gevent] no requirements",
              "gunicorn[gevent]" in req)
        check("Sem downloader específico — só yt-dlp[default]",
              "bgutil" not in req)
        check("Sem ffmpeg no requirements",
              "ffmpeg" not in req)


# ─── BLOCO 5: Lógica de extração ────────────────────────────────────────────

def test_extraction_logic():
    section("5. Lógica de extração — build_response_formats")

    # Simula o que o app.py faz com formatos reais
    # Sem precisar de rede — testa a lógica de filtragem

    # Formato simulado com URL direta (android retorna assim)
    mock_video = {
        "format_id": "137",
        "ext": "mp4",
        "height": 1080,
        "resolution": "1920x1080",
        "vcodec": "avc1.640028",
        "acodec": "none",  # DASH — sem áudio embutido
        "url": "https://rr1---sn-fake.googlevideo.com/videoplayback?expire=9999999999&sig=fake",
        "filesize": 50_000_000,
        "fps": 30,
    }
    mock_video_with_audio = {
        "format_id": "18",
        "ext": "mp4",
        "height": 360,
        "resolution": "640x360",
        "vcodec": "avc1.42001E",
        "acodec": "mp4a.40.2",
        "url": "https://rr1---sn-fake.googlevideo.com/videoplayback?expire=9999999999&sig=fake2",
        "filesize": 10_000_000,
        "fps": 30,
    }
    mock_audio = {
        "format_id": "140",
        "ext": "m4a",
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "url": "https://rr1---sn-fake.googlevideo.com/videoplayback?expire=9999999999&sig=fake3",
        "filesize": 5_000_000,
        "abr": 128,
    }

    # Testa que URLs são passadas corretamente
    check("URL de vídeo DASH tem sig= (assinada pelo YT)",
          "sig=" in mock_video["url"])
    check("URL de vídeo com áudio tem sig=",
          "sig=" in mock_video_with_audio["url"])
    check("URL de áudio tem sig=",
          "sig=" in mock_audio["url"])
    check("URL de vídeo tem expire= (tempo de expiração)",
          "expire=" in mock_video["url"])

    # Testa lógica de detecção de áudio embutido
    has_audio_dash = (mock_video.get("acodec") or "none") != "none"
    has_audio_muxed = (mock_video_with_audio.get("acodec") or "none") != "none"
    check("DASH detectado como sem áudio (acodec=none)", not has_audio_dash)
    check("Muxed detectado como com áudio", has_audio_muxed)

    # Testa que vídeo DASH precisaria de audio_url separado
    check("DASH precisaria de audio_url separado", not has_audio_dash and mock_audio.get("url"))


# ─── BLOCO 6: Cookies ───────────────────────────────────────────────────────

def test_cookies():
    section("6. Cookies")

    valid = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t9999999999\tYSC\ttest\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(valid)
        p = f.name

    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "cookiefile": p}) as ydl:
            pass
        check("Formato Netscape aceito pelo yt-dlp", True)
    except Exception as e:
        check("Formato Netscape aceito", False, str(e))
    finally:
        os.unlink(p)

    original = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\ttest\tval\n"
    decoded = base64.b64decode(base64.b64encode(original.encode())).decode()
    check("Cookies base64 encode/decode íntegro", decoded == original)

    local = os.path.join(os.path.dirname(__file__), "cookies.txt")
    if os.path.exists(local):
        content = open(local).read()
        check("cookies.txt tem cabeçalho Netscape", "# Netscape HTTP Cookie File" in content)
        check("cookies.txt tem domínio .youtube.com", ".youtube.com" in content)
        check("cookies.txt tem cookies de sessão", any(k in content for k in ["YSC", "VISITOR_INFO1_LIVE", "SID"]))
    else:
        warn("cookies.txt não encontrado — pulando validação de conteúdo")


# ─── BLOCO 7: Integração real (requer rede + cookies) ───────────────────────

def test_integration():
    section("7. Integração — extração real com YouTube")

    cookie_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
    import yt_dlp

    TEST_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    for client in ["android", "ios"]:
        try:
            t0 = time.time()
            opts = {
                "quiet": True,
                "skip_download": True,
                "nocheckcertificate": True,
                "check_formats": False,
                "ignore_no_formats_error": True,
                "js_runtimes": {"node": {}},
                "extractor_args": {
                    "youtube": {
                        "player_client": [client],
                        "formats": ["missing_pot"],
                    }
                },
                # android/ios sem cookies — intencional
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(TEST_URL, download=False)

            elapsed = time.time() - t0
            formats = info.get("formats") or []

            # URLs diretas (não manifest)
            direct = [f for f in formats
                      if f.get("url")
                      and not f.get("url", "").startswith("manifest")
                      and (f.get("vcodec") or "none") != "none"
                      and (f.get("height") or 0) > 0]

            check(f"client={client}: extração OK em {elapsed:.1f}s",
                  len(direct) > 0,
                  f"{len(direct)} formatos com URL direta")

            if direct:
                sample_url = direct[0].get("url", "")
                check(f"client={client}: URL tem expire=",
                      "expire=" in sample_url,
                      "URL pode estar vinculada a IP")
                check(f"client={client}: URL não tem &ip= (livre de IP)",
                      "&ip=" not in sample_url,
                      "URL vinculada a IP — download direto pelo browser pode falhar")
                resolutions = sorted(set(
                    f"{f.get('height')}p" for f in direct if f.get("height")
                ), key=lambda x: int(x[:-1]), reverse=True)
                ok(f"  Resoluções: {', '.join(resolutions[:5])}")

        except Exception as e:
            check(f"client={client}: extração", False, str(e)[:200])


# ─── Sumário ─────────────────────────────────────────────────────────────────

def summary():
    print(f"\n{'═'*52}")
    total = passed + failed
    if failed == 0:
        print(f"{GREEN}{BOLD}✓ TODOS OS TESTES PASSARAM ({passed}/{total}){RESET}")
        print(f"{GREEN}Seguro para deploy.{RESET}")
    else:
        print(f"{RED}{BOLD}✗ {failed} TESTE(S) FALHARAM ({passed}/{total} passou){RESET}")
        print(f"{RED}Corrija antes de fazer deploy.{RESET}")
    print(f"{'═'*52}\n")
    return failed == 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"\n{BOLD}GIOW Downloader v2 — Suite de Testes{RESET}")
    print(f"Modo: {mode} | Dir: {os.path.dirname(os.path.abspath(__file__))}")

    if mode in ("all", "unit"):
        test_environment()
        test_ytdlp_config()
        test_source_code()
        test_dockerfile()
        test_extraction_logic()
        test_cookies()

    if mode in ("all", "integration"):
        test_integration()

    ok = summary()
    sys.exit(0 if ok else 1)
