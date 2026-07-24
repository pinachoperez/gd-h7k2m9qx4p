#!/usr/bin/env python3
"""
Genera el feed RSS y sirve el podcast en la red local para Apple Podcasts.

Un solo feed.xml con todos los temas. Las pubDate se escalonan por bloque:
  grupo 01 (más reciente) → arriba; luego 02; luego 09; etc.
Así en Podcasts, ordenando por fecha, cada tema queda como un bloque contiguo.

1. Mac e iPhone en la misma Wi‑Fi
2. Ejecuta este script
3. En el iPhone: Podcasts → Buscar → "Seguir un programa mediante su URL"
4. Pega la URL del feed que imprime este script

Uso:
  python servir_podcast.py
  python servir_podcast.py --port 8080
  python servir_podcast.py --solo-rss --base-url http://192.168.1.20:8080
"""

from __future__ import annotations

import argparse
import email.utils
import html
import re
import socket
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from mutagen.mp4 import MP4

from generar_podcasts import (
    ARTIST,
    CATEGORY,
    COPYRIGHT,
    DEFAULT_COVER,
    DESCRIPTION,
    DIR,
    SHOW_NAME,
    asegurar_cover_carpeta,
    listar_audios_en,
    listar_carpetas_tema,
    nombre_tema,
    orden_grupo,
    preparar_portada,
    titulo_desde_archivo,
)

COVER_RSS = DIR / "cover.jpg"
FEED_PATH = DIR / "feed.xml"

# Huecos de fecha: cada grupo cabe entero antes del siguiente
DIAS_ENTRE_GRUPOS = 365
DIAS_ENTRE_EPISODIOS = 1

MIME = {
    ".xml": "application/rss+xml; charset=utf-8",
    ".m4a": "audio/x-m4a",
    ".mp3": "audio/mpeg",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def url_join(base: str, *parts: str) -> str:
    """Une base + segmentos de ruta (cada uno URL-encoded).

    Normaliza a NFC: macOS guarda NFD (ó = o + ́) y GitHub Pages sirve NFC;
    sin esto, Apple Podcasts ve 404 en los enclosures y falla en silencio.
    """
    segs = [quote(unicodedata.normalize("NFC", p)) for p in parts if p]
    return f"{base.rstrip('/')}/{'/'.join(segs)}"


def url_de_path(base: str, path: Path) -> str:
    rel = path.resolve().relative_to(DIR.resolve())
    return url_join(base, *rel.parts)


def guid_episodio(path: Path, tema: str) -> str:
    """GUID estable por tema + título (no cambia al renumerar el archivo)."""
    clave = re.sub(r"\s+", "-", titulo_desde_archivo(path).casefold())
    clave = re.sub(r"[^a-z0-9\-]+", "", clave).strip("-")
    tema_clave = re.sub(r"\s+", "-", tema.casefold())
    tema_clave = re.sub(r"[^a-z0-9\-]+", "", tema_clave).strip("-")
    return f"gurus-del-humo-{tema_clave}-{clave or path.stem}"


def duracion_segundos(path: Path) -> int:
    try:
        info = MP4(str(path)).info
        if info and info.length:
            return int(round(info.length))
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["afinfo", str(path)], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "estimated duration:" in line:
                return int(round(float(line.split(":")[1].split()[0])))
    except Exception:
        pass
    return 0


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def rfc2822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return email.utils.format_datetime(dt.astimezone(timezone.utc))


def numero_episodio(path: Path, fallback: int) -> int:
    m = re.match(r"^(\d+)\s*-\s*", path.stem)
    return int(m.group(1)) if m else fallback


def carpetas_ordenadas() -> list[Path]:
    """Carpetas de tema ordenadas por prefijo 01, 02, 09… (XXXXX al final)."""
    return sorted(listar_carpetas_tema(), key=orden_grupo)


def listar_episodios_fechados() -> list[tuple[Path, str, int, Path, datetime]]:
    """
    Episodios con pubDate artificial:
      - Grupo con prefijo menor (01) = bloque más reciente (arriba)
      - Dentro del grupo: ep 1 arriba, luego 2, 3…
    Devuelve (audio, tema, season, cover, pubDate).
    """
    carpetas = carpetas_ordenadas()
    if not carpetas:
        raise FileNotFoundError("No hay carpetas de tema con episodios.")

    ahora = datetime.now(tz=timezone.utc).replace(microsecond=0)
    out: list[tuple[Path, str, int, Path, datetime]] = []

    for g_idx, carpeta in enumerate(carpetas):
        tema = nombre_tema(carpeta)
        cover = asegurar_cover_carpeta(carpeta, cover_raiz=DEFAULT_COVER)
        audios = listar_audios_en(carpeta)
        # Tope del bloque: más nuevo que todo el grupo siguiente
        tope = ahora - timedelta(days=g_idx * DIAS_ENTRE_GRUPOS)
        for i, ep in enumerate(audios):
            # Ep 1 = más reciente del bloque → queda arriba dentro del grupo
            pub = tope - timedelta(days=i * DIAS_ENTRE_EPISODIOS)
            out.append((ep, tema, g_idx + 1, cover, pub))

    if not out:
        raise FileNotFoundError("No hay archivos .m4a en las carpetas de tema.")
    return out


def generar_feed(base_url: str, show: str, artist: str, description: str) -> str:
    episodios = listar_episodios_fechados()
    # Newest first en el XML (Apple / clientes lo esperan)
    episodios = sorted(episodios, key=lambda x: x[4], reverse=True)

    cover_url = url_join(base_url, COVER_RSS.name)
    now = rfc2822(datetime.now(tz=timezone.utc))
    link = base_url.rstrip("/")

    items = []
    for ep, tema, season, cover, pub_dt in episodios:
        n = numero_episodio(ep, 1)
        title = f"{tema} · {n} - {titulo_desde_archivo(ep)}"
        length = ep.stat().st_size
        secs = duracion_segundos(ep)
        enclosure = url_de_path(base_url, ep)
        ep_cover = url_de_path(base_url, cover)
        guid = guid_episodio(ep, tema)
        pub = rfc2822(pub_dt)
        items.append(
            f"""    <item>
      <title>{esc(title)}</title>
      <itunes:title>{esc(title)}</itunes:title>
      <description>{esc(description)}</description>
      <itunes:summary>{esc(description)}</itunes:summary>
      <enclosure url="{esc(enclosure)}" length="{length}" type="audio/x-m4a"/>
      <guid isPermaLink="false">{esc(guid)}</guid>
      <pubDate>{pub}</pubDate>
      <itunes:duration>{fmt_duration(secs)}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:episode>{n}</itunes:episode>
      <itunes:season>{season}</itunes:season>
      <itunes:author>{esc(artist)}</itunes:author>
      <itunes:image href="{esc(ep_cover)}"/>
      <itunes:keywords>{esc(tema)}</itunes:keywords>
    </item>"""
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{esc(show)}</title>
    <link>{esc(link)}</link>
    <description>{esc(description)}</description>
    <language>es-mx</language>
    <copyright>{esc(COPYRIGHT)}</copyright>
    <lastBuildDate>{now}</lastBuildDate>
    <pubDate>{now}</pubDate>
    <atom:link href="{esc(url_join(base_url, FEED_PATH.name))}" rel="self" type="application/rss+xml"/>
    <itunes:author>{esc(artist)}</itunes:author>
    <itunes:summary>{esc(description)}</itunes:summary>
    <itunes:type>episodic</itunes:type>
    <itunes:owner>
      <itunes:name>{esc(artist)}</itunes:name>
      <itunes:email>podcast@liverpool.com.mx</itunes:email>
    </itunes:owner>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="{esc(CATEGORY)}"/>
    <itunes:image href="{esc(cover_url)}"/>
    <image>
      <url>{esc(cover_url)}</url>
      <title>{esc(show)}</title>
      <link>{esc(link)}</link>
    </image>
{chr(10).join(items)}
  </channel>
</rss>
"""


def escribir_rss(
    base_url: str,
    *,
    show: str = SHOW_NAME,
    artist: str = ARTIST,
    description: str = DESCRIPTION,
    cover: Path = DEFAULT_COVER,
) -> int:
    if not cover.is_file():
        print(f"ERROR: no encuentro la portada: {cover}", file=sys.stderr)
        return 1

    print("Generando cover.jpg (canal)…")
    COVER_RSS.write_bytes(preparar_portada(cover))

    for carpeta in carpetas_ordenadas():
        c = asegurar_cover_carpeta(carpeta, cover_raiz=cover)
        print(f"  cover tema → {c.relative_to(DIR)}")

    print("Generando feed.xml (un feed; bloques por fecha / prefijo)…")
    FEED_PATH.write_text(
        generar_feed(base_url, show, artist, description), encoding="utf-8"
    )

    eps = listar_episodios_fechados()
    print(f"OK → {FEED_PATH.name}")
    print(f"OK → {COVER_RSS.name}")
    print(f"Temas: {len(carpetas_ordenadas())} · Episodios: {len(eps)}")
    print()
    print("Orden de bloques (arriba → abajo en Podcasts por fecha):")
    for carpeta in carpetas_ordenadas():
        n = len(listar_audios_en(carpeta))
        print(f"  · {nombre_tema(carpeta)} ({carpeta.name}) — {n} ep.")
    print(f"URL del feed: {base_url.rstrip('/')}/{FEED_PATH.name}")
    return 0


class PodcastHandler(SimpleHTTPRequestHandler):
    """Sirve el feed y audios; tolera clientes que cortan la descarga (Apple Podcasts)."""

    protocol_version = "HTTP/1.1"

    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def guess_type(self, path: str):
        ext = Path(path).suffix.lower()
        if ext in MIME:
            return MIME[ext]
        return super().guess_type(path)

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def copyfile(self, source, outputfile) -> None:
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def local_ip() -> str:
    for iface in ("en0", "en1"):
        try:
            out = subprocess.check_output(
                ["ipconfig", "getifaddr", iface], text=True
            ).strip()
            if out:
                return out
        except Exception:
            pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


CERT_DIR = DIR / ".podcast_certs"


def asegurar_certificado(ip: str) -> tuple[Path, Path]:
    """Crea certificado auto-firmado (SAN con IP + localhost) si no existe."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert = CERT_DIR / "cert.pem"
    key = CERT_DIR / "key.pem"
    if cert.is_file() and key.is_file():
        return cert, key

    conf = CERT_DIR / "openssl.cnf"
    conf.write_text(
        f"""[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = Gurús del Humo Local

[v3_req]
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
IP.2 = {ip}
""",
        encoding="utf-8",
    )
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "825",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-config",
            str(conf),
            "-extensions",
            "v3_req",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert, key


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Genera RSS y sirve Gurús del Humo para Apple Podcasts"
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--https",
        action="store_true",
        help="Sirve con HTTPS auto-firmado (a veces ayuda en iPhone)",
    )
    parser.add_argument(
        "--solo-rss",
        action="store_true",
        help="Solo genera feed.xml (no arranca el servidor)",
    )
    parser.add_argument(
        "--base-url",
        help="URL base para --solo-rss (ej. http://192.168.1.20:8080)",
    )
    parser.add_argument("--show", default=SHOW_NAME)
    parser.add_argument("--artist", default=ARTIST)
    parser.add_argument("--description", default=DESCRIPTION)
    parser.add_argument("--cover", type=Path, default=DEFAULT_COVER)
    args = parser.parse_args()

    if args.solo_rss:
        if not args.base_url:
            print("ERROR: --solo-rss requiere --base-url", file=sys.stderr)
            return 1
        return escribir_rss(
            args.base_url,
            show=args.show,
            artist=args.artist,
            description=args.description,
            cover=args.cover,
        )

    ip = local_ip()
    scheme = "https" if args.https else "http"
    base_url = f"{scheme}://{ip}:{args.port}"
    code = escribir_rss(
        base_url,
        show=args.show,
        artist=args.artist,
        description=args.description,
        cover=args.cover,
    )
    if code != 0:
        return code

    feed_url = f"{base_url}/{FEED_PATH.name}"
    feed_mac = f"{scheme}://127.0.0.1:{args.port}/{FEED_PATH.name}"
    handler = partial(PodcastHandler, directory=str(DIR))
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, args.port), handler)

    if args.https:
        import ssl

        cert, key = asegurar_certificado(ip)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    print()
    print("=" * 60)
    print("  PODCAST LISTO PARA APPLE PODCASTS")
    print("=" * 60)
    print()
    print("  Un feed.xml · temas en bloques por fecha (01 arriba).")
    print()
    print("  >>> En Podcasts de ESTE Mac, pega ESTA URL:")
    print()
    print(f"     {feed_mac}")
    print()
    print("  (No uses la IP 192.168… en el Mac: Podcasts la bloquea.)")
    print()
    print("  En el iPhone (misma Wi‑Fi):")
    print()
    print(f"     {feed_url}")
    print()
    print("  Deja esta ventana abierta mientras escuchas.")
    print("  Ctrl+C para detener el servidor.")
    print("=" * 60)
    print()

    try:
        sys.stdout.flush()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
