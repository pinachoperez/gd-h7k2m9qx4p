#!/usr/bin/env python3
"""
Prepara episodios .m4a para Apple / iPhone como podcast, agrupados por tema:

  - Carpetas: "lo-que-tu-pongas - NN" (NN = episodios al FINAL; el prefijo no se toca)
  - Episodios numerados sucesivos: "1 - Título.m4a" … "N - …"
  - Portada por carpeta: imagen local → cover.jpg; si no hay, copia la de raíz
  - Metadatos Apple (stik=21, pcst, covr, show)

Uso:
  python generar_podcasts.py              # procesa todas las carpetas de tema
  python generar_podcasts.py --copy       # copia a listos_para_airdrop
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from PIL import Image

# --- Identidad del podcast (portada Gurús del Humo / Liverpool México) ---
SHOW_NAME = "Gurús del Humo"
ARTIST = "Liverpool México"
ALBUM_ARTIST = "Liverpool México"
GENRE = "Podcast"
CATEGORY = "Technology"
DESCRIPTION = (
    "Gurús del Humo. Para desmentir mitos del sector. Podcast de Liverpool México."
)
COPYRIGHT = "© Liverpool México"

DIR = Path(__file__).resolve().parent
DEFAULT_COVER = DIR / "cover.jpg"
DEFAULT_OUT = DIR / "listos_para_airdrop"

# Carpetas / archivos que no son temas del podcast
_IGNORAR_DIRS = {
    ".venv",
    "listos_para_airdrop",
    ".podcast_certs",
    "__pycache__",
}

# Apple iTunes media kind: 21 = Podcast
STIK_PODCAST = 21
# Tamaño de portada recomendado por Apple Podcasts
COVER_SIZE = 3000
COVER_JPEG_QUALITY = 90

_PREFIXO_NUM = re.compile(r"^\d+\s*-\s*")
# Conteo de episodios al FINAL: "09 - LLM  y GEO - 88" → base + 88
_CARPETA_CONTEO_FINAL = re.compile(r"^(.*?)\s*-\s*(\d+)$")
_TEMP_AUDIO = re.compile(
    r"^(?:\.__ep_\d+__|tmp[a-z0-9_]+)$",
    re.IGNORECASE,
)
_IMAGEN_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def titulo_desde_archivo(path: Path) -> str:
    """Título limpio (sin prefijo '1 - ') a partir del nombre del archivo."""
    stem = path.stem
    stem = re.sub(r"^\.__ep_\d+__$", "", stem).strip("._ ")
    stem = _PREFIXO_NUM.sub("", stem).strip()
    stem = stem.replace("_", " ").strip(" .")
    return stem or path.stem


def es_audio_temporal(path: Path) -> bool:
    """True para .m4a temporales (renombrado in-place o afconvert)."""
    name = path.name
    if name.startswith("."):
        return True
    return bool(_TEMP_AUDIO.match(path.stem))


def nombre_base_carpeta(carpeta: Path) -> str:
    """
    Prefijo que controla el usuario (sin el conteo final).
    '09 - LLM  y GEO - 88' → '09 - LLM  y GEO'
    '03 - SEM' (aún sin conteo) → '03 - SEM'
    """
    m = _CARPETA_CONTEO_FINAL.match(carpeta.name)
    if m:
        return m.group(1).strip()
    return carpeta.name.strip()


def nombre_tema(carpeta: Path) -> str:
    """
    Nombre amigable del tema para metadatos/RSS.
    '09 - GEO & AEO - 07' → 'GEO & AEO'
    'XXXXX - ASO - 01' → 'ASO'
    """
    base = nombre_base_carpeta(carpeta)
    m = re.match(r"^\d+\s*-\s*(.+)$", base)
    if m:
        return m.group(1).strip()
    m = re.match(r"^.+?\s*-\s*(.+)$", base)
    if m:
        return m.group(1).strip()
    return base


def orden_grupo(carpeta: Path) -> tuple[int, int, str]:
    """
    Orden de grupos por prefijo numérico del usuario (01, 02, 09…).
    Sin número (p. ej. XXXXX) van al final.
    """
    base = nombre_base_carpeta(carpeta)
    m = re.match(r"^(\d+)", base.strip())
    if m:
        return (0, int(m.group(1)), base.casefold())
    return (1, 0, base.casefold())


def nombre_carpeta_tema(base: str, count: int) -> str:
    """Conserva el prefijo del usuario y pone el conteo al final: '… - 03'."""
    seguro = re.sub(r'[\\/:*?"<>|]', "-", base).strip(" .")
    return f"{seguro} - {count:02d}"


def listar_carpetas_tema(root: Path | None = None) -> list[Path]:
    """Carpetas de tema en la raíz (excluye venv, salidas, ocultas)."""
    root_dir = root or DIR
    out: list[Path] = []
    for p in sorted(root_dir.iterdir(), key=orden_grupo):
        if not p.is_dir():
            continue
        if p.name.startswith(".") or p.name in _IGNORAR_DIRS:
            continue
        out.append(p)
    return out


def _afinfo_texto(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["afinfo", str(path)], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return ""


def necesita_optimizar(path: Path) -> bool:
    """True si es MP4 fragmentado (mp4f) o no tiene fast-start (moov al final)."""
    info = _afinfo_texto(path)
    if not info:
        return True
    if "File type ID:" in info and "mp4f" in info:
        return True
    if "not optimized" in info:
        return True
    return False


def optimizar_m4a(path: Path, *, bitrate: int = 128_000) -> None:
    """
    Re-empaqueta a MPEG-4 Audio (m4af) AAC optimizado para Apple Podcasts.

    Los .m4a fragmentados (mp4f, tipicos de export web/IA) provocan
    "This episode can't be played on this device" aunque el feed cargue bien.
    """
    if not necesita_optimizar(path):
        return

    with tempfile.NamedTemporaryFile(
        suffix=".m4a", delete=False, dir=None
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        cmd = [
            "afconvert",
            str(path),
            str(tmp_path),
            "-d",
            "aac",
            "-f",
            "m4af",
            "-b",
            str(bitrate),
            "-q",
            "127",
            "-s",
            "3",
        ]
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def nombre_archivo_episodio(num: int, path: Path) -> str:
    """Nombre de archivo numerado: '1 - Título.m4a'."""
    titulo = titulo_desde_archivo(path)
    seguro = re.sub(r'[\\/:*?"<>|]', "-", titulo).strip(" .")
    return f"{num} - {seguro}.m4a"


def preparar_portada(cover_path: Path) -> bytes:
    """Convierte la portada a JPEG cuadrado (máx. 3000px) para covr de M4A."""
    with Image.open(cover_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
        if side > COVER_SIZE:
            im = im.resize((COVER_SIZE, COVER_SIZE), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=COVER_JPEG_QUALITY, optimize=True)
        return buf.getvalue()


def _buscar_imagen_portada(carpeta: Path) -> Path | None:
    """Prioriza cover.*; si no, la primera imagen no-audio de la carpeta."""
    for nombre in ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp"):
        cand = carpeta / nombre
        if cand.is_file():
            return cand
    imgs = sorted(
        p
        for p in carpeta.iterdir()
        if p.is_file()
        and p.suffix.lower() in _IMAGEN_EXTS
        and not p.name.startswith(".")
    )
    return imgs[0] if imgs else None


def asegurar_cover_carpeta(
    carpeta: Path, *, cover_raiz: Path = DEFAULT_COVER
) -> Path:
    """
    Deja cover.jpg en la carpeta:
      - Si hay imagen local → la convierte/renombra a cover.jpg
      - Si no → copia la portada de raíz
    """
    destino = carpeta / "cover.jpg"
    encontrada = _buscar_imagen_portada(carpeta)

    if encontrada is not None:
        jpeg = preparar_portada(encontrada)
        destino.write_bytes(jpeg)
        # Limpia la imagen original si no era ya cover.jpg
        if encontrada.resolve() != destino.resolve():
            try:
                encontrada.unlink()
            except OSError:
                pass
        return destino

    if not cover_raiz.is_file():
        raise FileNotFoundError(
            f"No hay imagen en {carpeta.name} ni portada de raíz: {cover_raiz}"
        )

    jpeg = preparar_portada(cover_raiz)
    destino.write_bytes(jpeg)
    return destino


def aplicar_metadatos_podcast(
    audio_path: Path,
    cover_jpeg: bytes,
    *,
    show: str,
    artist: str,
    episode_title: str | None = None,
    track: tuple[int, int] | None = None,
    description: str = DESCRIPTION,
) -> None:
    audio = MP4(str(audio_path))
    if audio.tags is None:
        audio.add_tags()

    title = episode_title or titulo_desde_archivo(audio_path)
    tags = audio.tags

    tags["\xa9nam"] = [title]  # título del episodio
    tags["\xa9alb"] = [show]  # nombre del podcast (álbum)
    tags["\xa9ART"] = [artist]
    tags["aART"] = [ALBUM_ARTIST]
    tags["\xa9gen"] = [GENRE]
    tags["\xa9cmt"] = [description]
    tags["desc"] = [description]
    tags["catg"] = [CATEGORY]
    tags["cprt"] = [COPYRIGHT]
    tags["tvsh"] = [show]  # show name (Apple)
    tags["pcst"] = [True]  # flag podcast
    tags["stik"] = [STIK_PODCAST]  # media kind = Podcast
    tags["covr"] = [MP4Cover(cover_jpeg, imageformat=MP4Cover.FORMAT_JPEG)]

    # GUID estable por episodio (útil si luego publicas RSS)
    tags["egid"] = [f"gurus-del-humo-{audio_path.stem}"]

    if track:
        tags["trkn"] = [track]

    # Agrupación: el iPhone agrupa episodios del mismo show
    tags["\xa9grp"] = [show]

    tags["----:com.apple.iTunes:MediaType"] = [
        MP4FreeForm(b"Podcast", dataformat=MP4FreeForm.FORMAT_TEXT)
    ]

    audio.save()


def _titulo_clave(path: Path) -> str:
    """Clave estable para deduplicar (sin número de episodio)."""
    t = titulo_desde_archivo(path).casefold()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _deduplicar_por_titulo(paths: list[Path]) -> list[Path]:
    """
    Si hay el mismo episodio numerado y sin numerar, conserva uno.
    Prefiere el número más bajo ya asignado; si no hay número, el más pequeño.
    """
    grupos: dict[str, list[Path]] = {}
    for p in paths:
        grupos.setdefault(_titulo_clave(p), []).append(p)

    elegidos: list[Path] = []
    for clave, grupo in grupos.items():
        if not clave:
            continue

        def score(p: Path) -> tuple[int, int, int]:
            m = _PREFIXO_NUM.match(p.stem)
            if m:
                num = int(re.match(r"^(\d+)", p.stem).group(1))
                return (1, -num, -p.stat().st_size)
            return (0, 0, -p.stat().st_size)

        mejor = max(grupo, key=score)
        elegidos.append(mejor)
    return elegidos


def _orden_renumerar(path: Path) -> tuple[int, float]:
    """
    Conserva el orden relativo de números existentes (2,5,6 → 1,2,3).
    Archivos nuevos sin número van al final por fecha de modificación.
    """
    m = _PREFIXO_NUM.match(path.stem)
    if m:
        num = int(re.match(r"^(\d+)", path.stem).group(1))
        return (0, float(num))
    return (1, path.stat().st_mtime)


def listar_audios_en(carpeta: Path) -> list[Path]:
    """Lista .m4a de una carpeta, ordenados para renumerar 1…N."""
    out = [
        p
        for p in carpeta.glob("*.m4a")
        if p.is_file() and not es_audio_temporal(p)
    ]
    out = _deduplicar_por_titulo(out)
    return sorted(out, key=_orden_renumerar)


def listar_audios(paths: list[Path] | None = None) -> list[Path]:
    """
    Lista todos los .m4a del podcast.
    - Si paths: esos archivos (compatibilidad CLI).
    - Si hay carpetas de tema: todos los de esas carpetas (por tema, luego nº).
    - Si no: .m4a sueltos en la raíz (legado).
    """
    if paths:
        out = []
        for p in paths:
            p = p.expanduser().resolve()
            if not p.is_file():
                raise FileNotFoundError(f"No existe: {p}")
            out.append(p)
        return out

    carpetas = listar_carpetas_tema()
    if carpetas:
        out: list[Path] = []
        for carpeta in carpetas:
            out.extend(listar_audios_en(carpeta))
        return out

    out = [
        p
        for p in DIR.glob("*.m4a")
        if p.is_file()
        and "listos_para_airdrop" not in str(p)
        and not es_audio_temporal(p)
    ]
    out = _deduplicar_por_titulo(out)
    return sorted(out, key=_orden_renumerar)


def limpiar_temporales_audio(directory: Path | None = None) -> list[Path]:
    """Elimina .m4a temporales huérfanos del renombrado / conversión."""
    root = directory or DIR
    borrados: list[Path] = []
    for p in root.glob("*.m4a"):
        if p.is_file() and es_audio_temporal(p):
            p.unlink()
            borrados.append(p)
    for p in root.glob(".__ep_*.m4a"):
        if p.is_file():
            p.unlink()
            borrados.append(p)
    return borrados


def renombrar_carpeta_con_conteo(carpeta: Path, count: int) -> Path:
    """
    Conserva el prefijo del usuario y actualiza solo el conteo al final.
    '09 - LLM  y GEO' + 7 eps → '09 - LLM  y GEO - 07'
    '09 - LLM  y GEO - 88' + 7 eps → '09 - LLM  y GEO - 07'
    """
    base = nombre_base_carpeta(carpeta)
    nuevo_nombre = nombre_carpeta_tema(base, count)
    if carpeta.name == nuevo_nombre:
        return carpeta
    destino = carpeta.with_name(nuevo_nombre)
    if destino.exists() and destino.resolve() != carpeta.resolve():
        raise FileExistsError(
            f"No puedo renombrar '{carpeta.name}' → '{nuevo_nombre}': ya existe."
        )
    carpeta.rename(destino)
    return destino


def procesar_carpeta(
    carpeta: Path,
    *,
    cover_raiz: Path,
    show: str,
    artist: str,
    in_place: bool,
    out_dir: Path | None,
) -> Path:
    """
    Asegura cover, renumeración 1…N, metadatos y conteo al final del nombre.
    Devuelve la ruta final de la carpeta (puede haber cambiado de nombre).
    """
    tema = nombre_tema(carpeta)
    base = nombre_base_carpeta(carpeta)
    print(f"\n=== Tema: {tema} ({carpeta.name}) ===")

    cover_path = asegurar_cover_carpeta(carpeta, cover_raiz=cover_raiz)
    print(f"Portada: {cover_path.relative_to(DIR)}")
    cover_jpeg = cover_path.read_bytes()
    print(f"  → {len(cover_jpeg) / 1024:.0f} KB JPEG")

    audios = listar_audios_en(carpeta)
    if not audios:
        print("  (sin .m4a)")
        return renombrar_carpeta_con_conteo(carpeta, 0)

    total = len(audios)
    print(f"Episodios: renumerando 1…{total}")

    plan: list[tuple[Path, int, str, str]] = []
    for i, src in enumerate(audios, start=1):
        titulo = f"{i} - {titulo_desde_archivo(src)}"
        nombre_final = nombre_archivo_episodio(i, src)
        plan.append((src, i, titulo, nombre_final))

    if in_place:
        nuevos: list[tuple[Path, int, str, str]] = []
        for src, i, titulo, nombre_final in plan:
            tmp = src.with_name(f".__ep_{i:03d}__.m4a")
            if tmp.exists():
                tmp.unlink()
            src.rename(tmp)
            nuevos.append((tmp, i, titulo, nombre_final))
        plan = nuevos
    else:
        assert out_dir is not None
        tema_out = out_dir / nombre_carpeta_tema(base, total)
        tema_out.mkdir(parents=True, exist_ok=True)
        for viejo in tema_out.glob("*.m4a"):
            viejo.unlink()
        # Copia cover de la carpeta
        shutil.copy2(cover_path, tema_out / "cover.jpg")

    for src, i, titulo, nombre_final in plan:
        if in_place:
            dest = src
            print(f"  [{i}/{total}] → {nombre_final}")
        else:
            assert out_dir is not None
            tema_out = out_dir / nombre_carpeta_tema(base, total)
            dest = tema_out / nombre_final
            print(f"  [{i}/{total}] Copiando → {nombre_final}")
            shutil.copy2(src, dest)

        if necesita_optimizar(dest):
            print("           Optimizando contenedor (mp4f → m4af)…")
            optimizar_m4a(dest)

        # Show con tema para agrupar en Música; el RSS sigue siendo un solo programa
        show_tema = f"{show} · {tema}"
        aplicar_metadatos_podcast(
            dest,
            cover_jpeg,
            show=show_tema,
            artist=artist,
            episode_title=titulo,
            track=(i, total),
        )

        if in_place:
            final = dest.with_name(nombre_final)
            if final.exists() and final.resolve() != dest.resolve():
                final.unlink()
            dest.rename(final)

    if in_place:
        return renombrar_carpeta_con_conteo(carpeta, total)

    assert out_dir is not None
    return out_dir / nombre_carpeta_tema(base, total)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Genera .m4a listos como podcast por carpetas de tema "
            "(numeración 1…N, portada cover.jpg, conteo al final: 'prefijo - NN')."
        )
    )
    parser.add_argument(
        "--cover",
        type=Path,
        default=DEFAULT_COVER,
        help="Portada de raíz (fallback si la carpeta no tiene imagen)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Si se indica, copia a esta carpeta en vez de renombrar los originales",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        default=True,
        help="Renombra y etiqueta los .m4a originales (default)",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copia a listos_para_airdrop en vez de renombrar originales",
    )
    parser.add_argument(
        "--show",
        default=SHOW_NAME,
        help=f'Nombre del podcast (default: "{SHOW_NAME}")',
    )
    parser.add_argument(
        "--artist",
        default=ARTIST,
        help=f'Artista / autor (default: "{ARTIST}")',
    )
    parser.add_argument(
        "--audio",
        type=Path,
        nargs="*",
        help="Uno o más .m4a concretos (salta el flujo por carpetas)",
    )
    parser.add_argument(
        "--carpeta",
        type=Path,
        nargs="*",
        help="Procesar solo estas carpetas de tema",
    )
    args = parser.parse_args()

    in_place = not args.copy and args.out is None
    out_dir = args.out or DEFAULT_OUT
    cover_raiz = args.cover.expanduser().resolve()

    if not cover_raiz.is_file():
        print(f"ERROR: no encuentro la portada de raíz: {cover_raiz}", file=sys.stderr)
        return 1

    # Limpieza de temporales en raíz y en cada tema
    borrados = limpiar_temporales_audio(DIR)
    for c in listar_carpetas_tema():
        borrados.extend(limpiar_temporales_audio(c))
    if borrados:
        print(f"Limpié {len(borrados)} temporal(es) huérfano(s).")

    # Modo legado: archivos sueltos pasados por --audio
    if args.audio:
        audios = listar_audios(args.audio)
        if not audios:
            print("No hay archivos .m4a para procesar.", file=sys.stderr)
            return 1
        cover_jpeg = preparar_portada(cover_raiz)
        total = len(audios)
        for i, src in enumerate(audios, start=1):
            nombre_final = nombre_archivo_episodio(i, src)
            if in_place:
                dest = src
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                dest = out_dir / nombre_final
                shutil.copy2(src, dest)
            if necesita_optimizar(dest):
                optimizar_m4a(dest)
            aplicar_metadatos_podcast(
                dest,
                cover_jpeg,
                show=args.show,
                artist=args.artist,
                episode_title=f"{i} - {titulo_desde_archivo(src)}",
                track=(i, total),
            )
            if in_place:
                final = dest.with_name(nombre_final)
                if final.exists() and final.resolve() != dest.resolve():
                    final.unlink()
                dest.rename(final)
                print(f"OK — {final.name}")
            else:
                print(f"OK — {dest.name}")
        return 0

    if args.carpeta:
        carpetas = [p.expanduser().resolve() for p in args.carpeta]
        for c in carpetas:
            if not c.is_dir():
                print(f"ERROR: no es carpeta: {c}", file=sys.stderr)
                return 1
    else:
        carpetas = listar_carpetas_tema()

    if not carpetas:
        # Legado: .m4a en la raíz sin carpetas de tema
        audios = listar_audios(None)
        if not audios:
            print(
                "No hay carpetas de tema ni .m4a en la raíz.",
                file=sys.stderr,
            )
            return 1
        print("Sin carpetas de tema: procesando .m4a de la raíz…")
        cover_jpeg = preparar_portada(cover_raiz)
        total = len(audios)
        plan: list[tuple[Path, int, str, str]] = []
        for i, src in enumerate(audios, start=1):
            plan.append(
                (src, i, f"{i} - {titulo_desde_archivo(src)}", nombre_archivo_episodio(i, src))
            )
        if in_place:
            nuevos = []
            for src, i, titulo, nombre_final in plan:
                tmp = src.with_name(f".__ep_{i:03d}__.m4a")
                if tmp.exists():
                    tmp.unlink()
                src.rename(tmp)
                nuevos.append((tmp, i, titulo, nombre_final))
            plan = nuevos
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            for viejo in out_dir.glob("*.m4a"):
                viejo.unlink()
        for src, i, titulo, nombre_final in plan:
            dest = src if in_place else out_dir / nombre_final
            if not in_place:
                shutil.copy2(src, dest)
            if necesita_optimizar(dest):
                optimizar_m4a(dest)
            aplicar_metadatos_podcast(
                dest,
                cover_jpeg,
                show=args.show,
                artist=args.artist,
                episode_title=titulo,
                track=(i, total),
            )
            if in_place:
                final = dest.with_name(nombre_final)
                if final.exists() and final.resolve() != dest.resolve():
                    final.unlink()
                dest.rename(final)
        print("Listo (raíz).")
        return 0

    if not in_place:
        out_dir.mkdir(parents=True, exist_ok=True)

    for carpeta in carpetas:
        # Re-lee por si un rename previo cambió paths hermanos (no aplica aquí)
        if not carpeta.exists():
            # Buscar por prefijo de carpeta tras renombres previos en la misma corrida
            base = nombre_base_carpeta(carpeta)
            hallada = next(
                (c for c in listar_carpetas_tema() if nombre_base_carpeta(c) == base),
                None,
            )
            if hallada is None:
                print(f"AVISO: desapareció {carpeta}", file=sys.stderr)
                continue
            carpeta = hallada
        procesar_carpeta(
            carpeta,
            cover_raiz=cover_raiz,
            show=args.show,
            artist=args.artist,
            in_place=in_place,
            out_dir=None if in_place else out_dir,
        )

    print()
    print("Listo.")
    if in_place:
        print("Carpetas de tema actualizadas (conteo + episodios 1…N + cover.jpg).")
    else:
        print(f"Copias en: {out_dir}")
    print()
    print("Siguiente paso: python servir_podcast.py  → un solo feed.xml con todos los temas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
