#!/usr/bin/env python3
"""
Genera el feed RSS y publica el podcast en GitHub Pages (Apple Podcasts).

Un solo feed.xml con todos los temas. Las pubDate se escalonan por bloque:
  grupo 01 (más reciente) → arriba; luego 02; luego 09; etc.

Flujo (por defecto):
  1. Genera cover.jpg + feed.xml con la URL de GitHub Pages
  2. git add / commit / push a origin
  3. Imprime la URL del feed para Apple Podcasts

Uso:
  python3 servir_podcast.py
  python3 servir_podcast.py --mensaje "Nuevos episodios SEM"
  python3 servir_podcast.py --solo-rss
  python3 servir_podcast.py --base-url https://USER.github.io/REPO
"""

from __future__ import annotations

import argparse
import email.utils
import html
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
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
ROBOTS_PATH = DIR / "robots.txt"
NOJEKYLL_PATH = DIR / ".nojekyll"

# Huecos de fecha: cada grupo cabe entero antes del siguiente
DIAS_ENTRE_GRUPOS = 365
DIAS_ENTRE_EPISODIOS = 1


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
        tope = ahora - timedelta(days=g_idx * DIAS_ENTRE_GRUPOS)
        for i, ep in enumerate(audios):
            pub = tope - timedelta(days=i * DIAS_ENTRE_EPISODIOS)
            out.append((ep, tema, g_idx + 1, cover, pub))

    if not out:
        raise FileNotFoundError("No hay archivos .m4a en las carpetas de tema.")
    return out


def generar_feed(base_url: str, show: str, artist: str, description: str) -> str:
    episodios = listar_episodios_fechados()
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


def asegurar_extras_pages() -> None:
    """Archivos útiles en GitHub Pages (anti-index + sin Jekyll)."""
    if not ROBOTS_PATH.is_file():
        ROBOTS_PATH.write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
        print(f"OK → {ROBOTS_PATH.name}")
    if not NOJEKYLL_PATH.is_file():
        NOJEKYLL_PATH.write_text("", encoding="utf-8")
        print(f"OK → {NOJEKYLL_PATH.name}")


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

    asegurar_extras_pages()

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


# --- GitHub Pages -----------------------------------------------------------

_GH_HTTPS = re.compile(
    r"(?:https://|git@)github\.com[:/](?P<user>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$"
)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=DIR,
        text=True,
        capture_output=True,
        check=check,
    )


def remoto_origin() -> str:
    try:
        out = git("remote", "get-url", "origin").stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "No hay remote 'origin'. Crea el repo en GitHub y:\n"
            "  git remote add origin https://github.com/USER/REPO.git"
        ) from e
    return out


def pages_base_url_desde_remote(remote: str | None = None) -> str:
    """https://USER.github.io/REPO a partir de origin."""
    url = (remote or remoto_origin()).strip()
    m = _GH_HTTPS.search(url)
    if not m:
        raise RuntimeError(
            f"No reconozco el remote de GitHub: {url}\n"
            "Usa --base-url https://USER.github.io/REPO"
        )
    user, repo = m.group("user"), m.group("repo")
    if repo.lower() == f"{user.lower()}.github.io":
        return f"https://{user}.github.io"
    return f"https://{user}.github.io/{repo}"


def hay_cambios_git() -> bool:
    staged = git("diff", "--cached", "--quiet", check=False).returncode != 0
    unstaged = git("diff", "--quiet", check=False).returncode != 0
    untracked = bool(git("ls-files", "--others", "--exclude-standard").stdout.strip())
    return staged or unstaged or untracked


def publicar_github(mensaje: str) -> int:
    """git add -A → commit (si hay cambios) → push origin HEAD."""
    if not (DIR / ".git").is_dir():
        print("ERROR: esta carpeta no es un repo git.", file=sys.stderr)
        return 1

    try:
        remoto_origin()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print()
    print("Publicando en GitHub…")
    git("add", "-A")

    if not hay_cambios_git():
        # Puede haber commit local sin push
        ahead = git(
            "rev-list", "--count", "@{u}..HEAD", check=False
        ).stdout.strip()
        if ahead and ahead != "0":
            print(f"Sin cambios nuevos; hay {ahead} commit(s) por subir.")
        else:
            print("Sin cambios que subir (ya está al día).")
            return 0
    else:
        try:
            git("commit", "-m", mensaje)
            print(f"Commit: {mensaje}")
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or "").strip()
            print(f"ERROR al hacer commit:\n{err}", file=sys.stderr)
            return 1

    branch = git("branch", "--show-current").stdout.strip() or "main"
    print(f"Push → origin/{branch} …")
    try:
        # HEAD: publica la rama actual (suele ser main)
        proc = subprocess.run(
            ["git", "push", "-u", "origin", "HEAD"],
            cwd=DIR,
            text=True,
            capture_output=True,
            check=True,
        )
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip())
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        print(f"ERROR en git push:\n{err}", file=sys.stderr)
        print(
            "\nSi pide login: gh auth login   o   usa un Personal Access Token.",
            file=sys.stderr,
        )
        return 1

    print("Push OK.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Genera RSS y publica Gurús del Humo en GitHub Pages"
    )
    parser.add_argument(
        "--solo-rss",
        action="store_true",
        help="Solo genera feed.xml (no hace commit ni push)",
    )
    parser.add_argument(
        "--base-url",
        help="URL base de Pages (por defecto: se deduce del remote origin)",
    )
    parser.add_argument(
        "--mensaje",
        "-m",
        default="Actualizar podcast en GitHub Pages",
        help="Mensaje del commit",
    )
    parser.add_argument("--show", default=SHOW_NAME)
    parser.add_argument("--artist", default=ARTIST)
    parser.add_argument("--description", default=DESCRIPTION)
    parser.add_argument("--cover", type=Path, default=DEFAULT_COVER)
    args = parser.parse_args()

    if args.base_url:
        base_url = args.base_url.rstrip("/")
    else:
        try:
            base_url = pages_base_url_desde_remote()
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    print(f"Base URL: {base_url}")
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

    if args.solo_rss:
        print()
        print("Modo --solo-rss: no se publicó en GitHub.")
        return 0

    code = publicar_github(args.mensaje)
    if code != 0:
        return code

    print()
    print("=" * 60)
    print("  PUBLICADO EN GITHUB PAGES")
    print("=" * 60)
    print()
    print("  En 1–2 min el feed queda vivo. En Podcasts pega:")
    print()
    print(f"     {feed_url}")
    print()
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
