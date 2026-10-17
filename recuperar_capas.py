import argparse
import glob
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field

import requests
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK
from mutagen.mp3 import MP3

DEFAULT_MUSIC_DIR = os.path.expanduser(r"~\Music")
DEFAULT_PROGRESS_FILE = "progresso.json"
DEFAULT_AUDIT_MD_FILE = "relatorio_audit.md"
DEFAULT_AUDIT_JSON_FILE = "relatorio_audit.json"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_SEARCH_URL = "https://api.deezer.com/search/album"
REQUEST_TIMEOUT = 10
REQUEST_DELAY_SECONDS = 1
ESSENTIAL_TAG_FIELDS = ("artist", "album", "track_number", "title")
logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    success: bool
    message: str
    should_record_progress: bool


@dataclass
class InferredMetadata:
    artist: str | None = None
    album: str | None = None
    track_number: str | None = None
    title: str | None = None
    confidence: str = "none"
    source: str = "none"


@dataclass
class TrackRecord:
    path: str
    folder: str
    filename: str
    artist_tag: str | None = None
    album_tag: str | None = None
    title_tag: str | None = None
    track_tag: str | None = None
    artist_inferred: str | None = None
    album_inferred: str | None = None
    title_inferred: str | None = None
    track_inferred: str | None = None
    inference_confidence: str = "none"
    inference_source: str = "none"
    has_cover: bool = False
    cover_fingerprint: str | None = None
    group_key: str | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def resolved_artist(self):
        return self.artist_tag or self.artist_inferred

    @property
    def resolved_album(self):
        return self.album_tag or self.album_inferred

    @property
    def resolved_title(self):
        return self.title_tag or self.title_inferred

    @property
    def resolved_track_number(self):
        return self.track_tag or self.track_inferred


@dataclass
class AuditSummary:
    scanned_files: int
    folders_with_mp3: int
    groups_found: int
    tracks_missing_tags: int
    tracks_missing_cover: int
    tracks_with_inferred_metadata: int
    mixed_folders: int
    groups: dict[str, list[TrackRecord]]
    mixed_folder_map: dict[str, list[str]]
    issue_counts: dict[str, int]


@dataclass
class TagFixResult:
    scanned_files: int = 0
    updated_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    updated_paths: list[str] = field(default_factory=list)
    skipped_details: list[str] = field(default_factory=list)
    failure_details: list[str] = field(default_factory=list)


@dataclass
class CoverFixResult:
    scanned_groups: int = 0
    updated_files: int = 0
    skipped_groups: int = 0
    failed_groups: int = 0
    updated_details: list[str] = field(default_factory=list)
    skipped_details: list[str] = field(default_factory=list)
    failure_details: list[str] = field(default_factory=list)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Recupera e embute capas ausentes em colecoes de arquivos MP3."
    )
    parser.add_argument(
        "--music-dir",
        default=os.environ.get("CAPASMP3_MUSIC_DIR", DEFAULT_MUSIC_DIR),
        help="Diretorio raiz contendo os arquivos MP3.",
    )
    parser.add_argument(
        "--progress-file",
        default=os.environ.get("CAPASMP3_PROGRESS_FILE", DEFAULT_PROGRESS_FILE),
        help="Arquivo JSON usado para registrar diretorios ja processados.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help="Intervalo em segundos entre diretorios processados.",
    )
    parser.add_argument(
        "--mode",
        choices=("cover", "audit", "fix-tags", "fix-covers"),
        default="cover",
        help="Modo de execucao: cover para recuperar capas, audit para diagnosticar, fix-tags para escrever tags ausentes, fix-covers para corrigir capas por album logico.",
    )
    parser.add_argument(
        "--audit-md-file",
        default=DEFAULT_AUDIT_MD_FILE,
        help="Relatorio Markdown gerado no modo audit.",
    )
    parser.add_argument(
        "--audit-json-file",
        default=DEFAULT_AUDIT_JSON_FILE,
        help="Relatorio JSON gerado no modo audit.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduz a saida para mostrar apenas erros.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Aumenta a verbosidade para incluir detalhes de depuracao.",
    )
    return parser.parse_args()


def configure_logging(args):
    level = logging.INFO
    if args.quiet:
        level = logging.ERROR
    elif args.verbose:
        level = logging.DEBUG

    logging.basicConfig(level=level, format="%(message)s")


def normalize_text(value):
    if not value:
        return None
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or None


def build_group_key(artist, album):
    normalized_artist = normalize_text(artist) or "desconhecido"
    normalized_album = normalize_text(album) or "sem-album"
    return f"{normalized_artist}::{normalized_album}"


def sanitize_filename_component(value):
    sanitized = re.sub(r'[<>:"/\\|?*]+', "_", value or "desconhecido")
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized or "desconhecido"


def build_group_cover_filename(record):
    artist = sanitize_filename_component(record.resolved_artist)
    album = sanitize_filename_component(record.resolved_album)
    return f"cover - {artist} - {album}.jpg"


def compute_image_fingerprint(image_data):
    return hashlib.sha1(image_data).hexdigest()[:12]


def notify_shell_file_updated(file_path):
    """Notifica o Windows Explorer que o conteudo do arquivo foi alterado.

    Sem essa chamada, o Explorer mantem a miniatura antiga em cache mesmo
    depois que a tag APIC e reescrita no disco, porque o thumbnail cache
    nao e invalidado automaticamente pela mutagen.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        SHCNE_UPDATEITEM = 0x00002000
        SHCNF_PATH = 0x0001
        SHCNF_FLUSHNOWAIT = 0x3000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_UPDATEITEM,
            SHCNF_PATH | SHCNF_FLUSHNOWAIT,
            ctypes.c_wchar_p(os.path.abspath(file_path)),
            None,
        )
    except Exception as exc:
        logger.debug("Nao foi possivel notificar o Explorer sobre %s: %s", file_path, exc)


def notify_shell_dir_updated(dir_path):
    """Notifica o Windows Explorer que o conteudo de um diretorio foi alterado."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        SHCNE_UPDATEDIR = 0x00001000
        SHCNF_PATH = 0x0001
        SHCNF_FLUSHNOWAIT = 0x3000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_UPDATEDIR,
            SHCNF_PATH | SHCNF_FLUSHNOWAIT,
            ctypes.c_wchar_p(os.path.abspath(dir_path)),
            None,
        )
    except Exception as exc:
        logger.debug("Nao foi possivel notificar o Explorer sobre o diretorio %s: %s", dir_path, exc)


def cleanup_stale_folder_art(folder):
    """Remove arquivos de arte de album obsoletos que o Windows usa no lugar da tag APIC.

    O Windows Media Player e o proprio Explorer gravam AlbumArt_*.jpg, Folder.jpg,
    AlbumArtSmall.jpg e cover.jpg na pasta de musicas. Quando esses arquivos existem,
    o Explorer exibe a imagem deles no painel de preview, IGNORANDO a tag APIC embutida
    no MP3. Em pastas com multiplos albuns logicos, isso faz todos os arquivos mostrarem
    a mesma capa errada independente do que esta embutido. Deletar esses arquivos forca
    o Explorer a ler a tag APIC de cada arquivo individualmente.
    """
    patterns = [
        os.path.join(folder, "AlbumArt_*.jpg"),
        os.path.join(folder, "AlbumArtSmall.jpg"),
        os.path.join(folder, "Folder.jpg"),
        os.path.join(folder, "cover.jpg"),
    ]
    removed = []
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                os.remove(path)
                removed.append(os.path.basename(path))
                logger.debug("Removido arquivo de arte obsoleto: %s", path)
            except OSError as exc:
                logger.warning("Nao foi possivel remover %s: %s", path, exc)
    if removed:
        logger.info("Arte de pasta obsoleta removida: %s", ", ".join(removed))
    return removed


def build_cover_search_candidates(record):
    candidates = []
    artist_candidates = [
        record.resolved_artist,
        record.artist_inferred,
        re.sub(r"^\s*grupo\s+", "", record.resolved_artist or "", flags=re.IGNORECASE),
    ]
    album_candidates = [record.resolved_album, record.album_inferred]

    for artist in artist_candidates:
        for album in album_candidates:
            artist = (artist or "").strip()
            album = (album or "").strip()
            if not album:
                continue
            candidate = (artist, album)
            if candidate not in candidates:
                candidates.append(candidate)

    album_only = ("", (record.resolved_album or record.album_inferred or "").strip())
    if album_only[1] and album_only not in candidates:
        candidates.append(album_only)
    return candidates


def load_progress(progress_file):
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
        except json.JSONDecodeError as exc:
            logger.warning("Progresso invalido em %s: %s", progress_file, exc)
            return set()
        except OSError as exc:
            logger.warning("Nao foi possivel ler %s: %s", progress_file, exc)
            return set()

        if isinstance(data, list):
            return {item for item in data if isinstance(item, str)}

        logger.warning("Formato inesperado em %s; reiniciando progresso.", progress_file)
    return set()


def save_progress(progress_file, progress_set):
    with open(progress_file, "w", encoding="utf-8") as file_obj:
        json.dump(sorted(progress_set), file_obj, indent=4, ensure_ascii=False)


def parse_filename_metadata(filename):
    stem, _ = os.path.splitext(os.path.basename(filename))
    parts = [part.strip() for part in re.split(r"\s*-\s*", stem) if part.strip()]
    if len(parts) < 4:
        return InferredMetadata()

    track_candidate = parts[-2]
    if not re.fullmatch(r"\d{1,3}", track_candidate):
        return InferredMetadata(confidence="low", source="filename")

    artist = parts[0]
    album = " - ".join(parts[1:-2]).strip() or None
    title = parts[-1]
    if not artist or not album or not title:
        return InferredMetadata(confidence="low", source="filename")

    return InferredMetadata(
        artist=artist,
        album=album,
        track_number=track_candidate,
        title=title,
        confidence="high",
        source="filename",
    )


def extract_cover_fingerprint(tags):
    if not tags:
        return None

    for tag in tags.values():
        if isinstance(tag, APIC):
            return hashlib.sha1(tag.data).hexdigest()[:12]
    return None


def read_track_metadata(mp3_path):
    inferred = parse_filename_metadata(os.path.basename(mp3_path))
    record = TrackRecord(
        path=mp3_path,
        folder=os.path.dirname(mp3_path),
        filename=os.path.basename(mp3_path),
        artist_inferred=inferred.artist,
        album_inferred=inferred.album,
        title_inferred=inferred.title,
        track_inferred=inferred.track_number,
        inference_confidence=inferred.confidence,
        inference_source=inferred.source,
    )

    try:
        audio = MP3(mp3_path, ID3=ID3)
        tags = audio.tags
        if tags:
            artist = tags.get("TPE1") or tags.get("TPE2")
            album = tags.get("TALB")
            title = tags.get("TIT2")
            track = tags.get("TRCK")

            record.artist_tag = artist.text[0].strip() if artist and artist.text else None
            record.album_tag = album.text[0].strip() if album and album.text else None
            record.title_tag = title.text[0].strip() if title and title.text else None
            record.track_tag = track.text[0].strip() if track and track.text else None
            record.has_cover = any(isinstance(tag, APIC) for tag in tags.values())
            record.cover_fingerprint = extract_cover_fingerprint(tags)
        else:
            record.issues.append("sem-tags")
    except Exception as exc:
        record.issues.append("erro-leitura-tags")
        logger.warning("Falha ao ler tags de %s: %s", mp3_path, exc)
        return record

    missing_fields = get_missing_tag_fields(record)

    if missing_fields:
        record.issues.append("tags-ausentes")
        for field_name in missing_fields:
            record.issues.append(f"tag-ausente:{field_name}")

    if record.inference_confidence == "high":
        record.issues.append("metadados-inferidos")
    elif record.inference_confidence == "low":
        record.issues.append("inferencia-fraca")

    if not record.has_cover:
        record.issues.append("sem-capa")

    resolved_artist = record.resolved_artist
    resolved_album = record.resolved_album
    record.group_key = build_group_key(resolved_artist, resolved_album)
    if not resolved_artist or not resolved_album:
        record.issues.append("grupo-incompleto")

    return record


def get_album_info(mp3_path):
    record = read_track_metadata(mp3_path)
    return record.resolved_artist, record.resolved_album


def has_embedded_cover(mp3_path):
    try:
        audio = MP3(mp3_path, ID3=ID3)
        if not audio.tags:
            return False
        return any(isinstance(tag, APIC) for tag in audio.tags.values())
    except Exception as exc:
        logger.warning("Falha ao inspecionar capa em %s: %s", mp3_path, exc)
        return False


def fetch_cover_url(artist, album):
    query_parts = [part.strip() for part in (artist, album) if part and part.strip()]
    query = " ".join(query_parts)
    if not query:
        return None, "Sem termos suficientes para buscar capa"

    params = {
        "term": query,
        "entity": "album",
        "limit": 1,
        "country": "br",
    }
    try:
        response = requests.get(ITUNES_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        return None, f"Erro de rede ao consultar iTunes: {exc}"
    except ValueError as exc:
        return None, f"Resposta invalida do iTunes: {exc}"

    if data.get("resultCount", 0) <= 0:
        return None, "Capa nao encontrada no iTunes"

    artwork_url = data["results"][0].get("artworkUrl100")
    if not artwork_url:
        return None, "Resultado do iTunes sem URL de capa"

    return artwork_url.replace("100x100bb", "600x600bb"), None


def fetch_cover_url_deezer(artist, album):
    query_parts = [part.strip() for part in (artist, album) if part and part.strip()]
    query = " ".join(query_parts)
    if not query:
        return None, "Sem termos suficientes para buscar capa no Deezer"

    params = {"q": query, "limit": 1}
    try:
        response = requests.get(DEEZER_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        return None, f"Erro de rede ao consultar Deezer: {exc}"
    except ValueError as exc:
        return None, f"Resposta invalida do Deezer: {exc}"

    results = data.get("data", [])
    if not results:
        return None, "Capa nao encontrada no Deezer"

    cover_url = results[0].get("cover_big") or results[0].get("cover_xl") or results[0].get("cover")
    if not cover_url:
        return None, "Resultado do Deezer sem URL de capa"

    return cover_url, None


def download_cover(cover_url):
    try:
        response = requests.get(cover_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.content, None
    except requests.RequestException as exc:
        return None, f"Erro ao baixar capa: {exc}"


def embed_cover(mp3_path, image_data, mime_type="image/jpeg"):
    try:
        audio = MP3(mp3_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()

        for key in list(audio.tags.keys()):
            if key.startswith("APIC"):
                del audio.tags[key]

        audio.tags.add(
            APIC(
                encoding=3,
                mime=mime_type,
                type=3,
                desc="Cover",
                data=image_data,
            )
        )
        audio.save()
        notify_shell_file_updated(mp3_path)
        return True, None
    except Exception as exc:
        return False, str(exc)


def classify_directory(mp3_files):
    covered = []
    missing_cover = []

    for mp3_path in mp3_files:
        if has_embedded_cover(mp3_path):
            covered.append(mp3_path)
        else:
            missing_cover.append(mp3_path)

    return covered, missing_cover


def scan_library(music_dir):
    records = []
    folders_with_mp3 = set()

    for root, _, files in os.walk(music_dir):
        mp3_files = [
            os.path.join(root, file_name)
            for file_name in files
            if file_name.lower().endswith(".mp3")
        ]
        if not mp3_files:
            continue

        folders_with_mp3.add(root)
        for mp3_path in sorted(mp3_files):
            records.append(read_track_metadata(mp3_path))

    return records, folders_with_mp3


def group_tracks(records):
    groups = {}
    for record in records:
        groups.setdefault(record.group_key or "desconhecido::sem-album", []).append(record)
    return groups


def detect_mixed_folders(records):
    folder_map = {}
    for record in records:
        folder_map.setdefault(record.folder, set()).add(record.group_key or "desconhecido::sem-album")
    return {
        folder: sorted(group_keys)
        for folder, group_keys in folder_map.items()
        if len(group_keys) > 1
    }


def build_issue_counts(records):
    issue_counts = {}
    for record in records:
        for issue in record.issues:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    return issue_counts


def build_audit_summary(records, folders_with_mp3):
    groups = group_tracks(records)
    mixed_folder_map = detect_mixed_folders(records)
    tracks_missing_tags = sum("tags-ausentes" in record.issues for record in records)
    tracks_missing_cover = sum("sem-capa" in record.issues for record in records)
    tracks_with_inferred_metadata = sum("metadados-inferidos" in record.issues for record in records)
    return AuditSummary(
        scanned_files=len(records),
        folders_with_mp3=len(folders_with_mp3),
        groups_found=len(groups),
        tracks_missing_tags=tracks_missing_tags,
        tracks_missing_cover=tracks_missing_cover,
        tracks_with_inferred_metadata=tracks_with_inferred_metadata,
        mixed_folders=len(mixed_folder_map),
        groups=groups,
        mixed_folder_map=mixed_folder_map,
        issue_counts=build_issue_counts(records),
    )


def get_missing_tag_fields(record):
    missing_fields = []
    if not record.artist_tag:
        missing_fields.append("artist")
    if not record.album_tag:
        missing_fields.append("album")
    if not record.track_tag:
        missing_fields.append("track_number")
    if not record.title_tag:
        missing_fields.append("title")
    return missing_fields


def can_write_inferred_tags(record):
    if record.inference_confidence != "high":
        return False
    return bool(get_missing_tag_fields(record))


def apply_inferred_tags(mp3_path, inferred):
    try:
        audio = MP3(mp3_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()

        updated_fields = []
        if inferred.artist and not audio.tags.get("TPE1"):
            audio.tags.add(TPE1(encoding=3, text=inferred.artist))
            updated_fields.append("artist")
        if inferred.album and not audio.tags.get("TALB"):
            audio.tags.add(TALB(encoding=3, text=inferred.album))
            updated_fields.append("album")
        if inferred.track_number and not audio.tags.get("TRCK"):
            audio.tags.add(TRCK(encoding=3, text=inferred.track_number))
            updated_fields.append("track_number")
        if inferred.title and not audio.tags.get("TIT2"):
            audio.tags.add(TIT2(encoding=3, text=inferred.title))
            updated_fields.append("title")

        if not updated_fields:
            return True, []

        audio.save()
        return True, updated_fields
    except Exception as exc:
        return False, str(exc)


def write_audit_json(output_path, summary):
    payload = {
        "summary": {
            "scanned_files": summary.scanned_files,
            "folders_with_mp3": summary.folders_with_mp3,
            "groups_found": summary.groups_found,
            "tracks_missing_tags": summary.tracks_missing_tags,
            "tracks_missing_cover": summary.tracks_missing_cover,
            "tracks_with_inferred_metadata": summary.tracks_with_inferred_metadata,
            "mixed_folders": summary.mixed_folders,
            "issue_counts": summary.issue_counts,
        },
        "mixed_folders": summary.mixed_folder_map,
        "groups": {
            group_key: [asdict(record) for record in records]
            for group_key, records in summary.groups.items()
        },
    }
    with open(output_path, "w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, indent=2, ensure_ascii=False)


def write_audit_markdown(output_path, summary):
    lines = [
        "# Relatorio de Audit do CapasMP3",
        "",
        "## Resumo",
        f"- Arquivos analisados: {summary.scanned_files}",
        f"- Pastas com MP3: {summary.folders_with_mp3}",
        f"- Grupos logicos encontrados: {summary.groups_found}",
        f"- Arquivos com tags ausentes: {summary.tracks_missing_tags}",
        f"- Arquivos sem capa: {summary.tracks_missing_cover}",
        f"- Arquivos com metadados inferidos: {summary.tracks_with_inferred_metadata}",
        f"- Pastas com multiplos albuns: {summary.mixed_folders}",
        "",
        "## Contagem de Problemas",
    ]

    if summary.issue_counts:
        for issue_name, count in sorted(summary.issue_counts.items()):
            lines.append(f"- `{issue_name}`: {count}")
    else:
        lines.append("- Nenhum problema detectado")

    lines.extend(["", "## Pastas Mistas"])
    if summary.mixed_folder_map:
        for folder, group_keys in sorted(summary.mixed_folder_map.items()):
            lines.append(f"- `{folder}`")
            lines.append(f"  - grupos: {', '.join(group_keys)}")
    else:
        lines.append("- Nenhuma pasta mista detectada")

    lines.extend(["", "## Grupos"])
    for group_key, records in sorted(summary.groups.items()):
        first_record = records[0]
        artist = first_record.resolved_artist or "Desconhecido"
        album = first_record.resolved_album or "Sem album"
        missing_cover_count = sum("sem-capa" in record.issues for record in records)
        missing_tag_count = sum("tags-ausentes" in record.issues for record in records)
        lines.append(f"### {artist} - {album}")
        lines.append(f"- Chave: `{group_key}`")
        lines.append(f"- Pasta: `{first_record.folder}`")
        lines.append(f"- Faixas: {len(records)}")
        lines.append(f"- Sem capa: {missing_cover_count}")
        lines.append(f"- Com tags ausentes: {missing_tag_count}")
        for record in records:
            issues_text = ", ".join(record.issues) if record.issues else "nenhuma"
            lines.append(
                f"- `{record.filename}` | artista=`{record.resolved_artist or '-'}"
                f"` | album=`{record.resolved_album or '-'}"
                f"` | faixa=`{record.resolved_track_number or '-'}"
                f"` | titulo=`{record.resolved_title or '-'}"
                f"` | issues={issues_text}"
            )
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as file_obj:
        file_obj.write("\n".join(lines).rstrip() + "\n")


def run_audit(args, music_dir):
    records, folders_with_mp3 = scan_library(music_dir)
    summary = build_audit_summary(records, folders_with_mp3)
    audit_md_file = os.path.abspath(args.audit_md_file)
    audit_json_file = os.path.abspath(args.audit_json_file)
    write_audit_markdown(audit_md_file, summary)
    write_audit_json(audit_json_file, summary)

    logger.info("Audit concluido.")
    logger.info("Arquivos analisados: %s", summary.scanned_files)
    logger.info("Grupos logicos encontrados: %s", summary.groups_found)
    logger.info("Pastas mistas detectadas: %s", summary.mixed_folders)
    logger.info("Relatorio Markdown: %s", audit_md_file)
    logger.info("Relatorio JSON: %s", audit_json_file)
    return 0


def run_fix_tags(music_dir):
    records, _ = scan_library(music_dir)
    result = TagFixResult(scanned_files=len(records))

    for record in records:
        if not can_write_inferred_tags(record):
            result.skipped_files += 1
            missing_fields = ", ".join(get_missing_tag_fields(record)) or "nenhuma"
            result.skipped_details.append(
                f"{record.path}: confianca={record.inference_confidence}, faltando={missing_fields}"
            )
            continue

        inferred = InferredMetadata(
            artist=record.artist_inferred,
            album=record.album_inferred,
            track_number=record.track_inferred,
            title=record.title_inferred,
            confidence=record.inference_confidence,
            source=record.inference_source,
        )
        success, payload = apply_inferred_tags(record.path, inferred)
        if success:
            updated_fields = payload
            if updated_fields:
                result.updated_files += 1
                result.updated_paths.append(f"{record.path}: {', '.join(updated_fields)}")
            else:
                result.skipped_files += 1
                result.skipped_details.append(f"{record.path}: nenhuma tag nova precisava ser escrita")
        else:
            result.failed_files += 1
            result.failure_details.append(f"{record.path}: {payload}")

    logger.info("Fix-tags concluido.")
    logger.info("Arquivos analisados: %s", result.scanned_files)
    logger.info("Arquivos atualizados: %s", result.updated_files)
    logger.info("Arquivos ignorados: %s", result.skipped_files)
    logger.info("Falhas: %s", result.failed_files)
    for detail in result.updated_paths:
        logger.info("Atualizado: %s", detail)
    for detail in result.failure_details:
        logger.error("Falha: %s", detail)
    return 0


def save_group_cover_file(folder, record, image_data):
    cover_filename = build_group_cover_filename(record)
    cover_path = os.path.join(folder, cover_filename)
    with open(cover_path, "wb") as file_obj:
        file_obj.write(image_data)
    return cover_path


def fix_group_covers(group_key, records):
    first_record = records[0]
    folder = first_record.folder

    if not first_record.resolved_album and not first_record.album_inferred:
        return False, "Grupo sem artista/album resolvidos", []

    cover_url = None
    cover_error = None
    for artist_candidate, album_candidate in build_cover_search_candidates(first_record):
        cover_url, cover_error = fetch_cover_url(artist_candidate, album_candidate)
        if cover_url:
            logger.debug(
                "[%s] Capa encontrada no iTunes com artista=%s album=%s",
                group_key,
                artist_candidate,
                album_candidate,
            )
            break

    if not cover_url:
        logger.debug("[%s] iTunes nao encontrou capa; tentando Deezer", group_key)
        for artist_candidate, album_candidate in build_cover_search_candidates(first_record):
            cover_url, cover_error = fetch_cover_url_deezer(artist_candidate, album_candidate)
            if cover_url:
                logger.debug(
                    "[%s] Capa encontrada no Deezer com artista=%s album=%s",
                    group_key,
                    artist_candidate,
                    album_candidate,
                )
                break

    if not cover_url:
        return False, cover_error or "Capa nao encontrada", []

    image_data, download_error = download_cover(cover_url)
    if download_error:
        return False, download_error, []

    expected_fingerprint = compute_image_fingerprint(image_data)

    try:
        save_group_cover_file(folder, first_record, image_data)
    except OSError as exc:
        return False, f"Erro ao salvar capa do grupo: {exc}", []

    updated_files = []
    for record in records:
        if record.cover_fingerprint == expected_fingerprint:
            continue

        success, error_message = embed_cover(record.path, image_data)
        if not success:
            return False, f"Falha ao embutir capa em {record.path}: {error_message}", updated_files
        updated_files.append(record.path)

    return True, None, updated_files


def run_fix_covers(music_dir):
    records, _ = scan_library(music_dir)
    groups = group_tracks(records)
    result = CoverFixResult(scanned_groups=len(groups))
    affected_folders = set()

    for group_key, group_records in sorted(groups.items()):
        first_record = group_records[0]
        if not first_record.resolved_artist or not first_record.resolved_album:
            result.skipped_groups += 1
            result.skipped_details.append(f"{group_key}: grupo sem artista/album resolvidos")
            continue

        success, error_message, updated_files = fix_group_covers(group_key, group_records)
        if success:
            if updated_files:
                result.updated_files += len(updated_files)
                result.updated_details.append(f"{group_key}: {len(updated_files)} arquivo(s) atualizados")
                affected_folders.add(first_record.folder)
            else:
                result.skipped_groups += 1
                result.skipped_details.append(f"{group_key}: capas ja estavam corretas")
        else:
            result.failed_groups += 1
            result.failure_details.append(f"{group_key}: {error_message}")

    for folder in affected_folders:
        cleanup_stale_folder_art(folder)
        notify_shell_dir_updated(folder)

    logger.info("Fix-covers concluido.")
    logger.info("Grupos analisados: %s", result.scanned_groups)
    logger.info("Arquivos com capa atualizada: %s", result.updated_files)
    logger.info("Grupos ignorados: %s", result.skipped_groups)
    logger.info("Grupos com falha: %s", result.failed_groups)
    for detail in result.updated_details:
        logger.info("Atualizado: %s", detail)
    for detail in result.skipped_details:
        logger.info("Ignorado: %s", detail)
    for detail in result.failure_details:
        logger.error("Falha: %s", detail)
    return 0 if result.failed_groups == 0 else 1


def process_directory(dir_path):
    mp3_files = glob.glob(os.path.join(dir_path, "*.mp3"))
    if len(mp3_files) < 2:
        return ProcessResult(False, "Avulso (menos de 2 musicas)", True)

    records = [read_track_metadata(mp3_path) for mp3_path in mp3_files]
    groups = group_tracks(records)
    if len(groups) > 1:
        return ProcessResult(
            False,
            "Pasta contem multiplos albuns logicos; execute em modo audit antes de corrigir capas",
            True,
        )

    group_records = next(iter(groups.values()))
    first_record = group_records[0]
    artist = first_record.resolved_artist
    album = first_record.resolved_album

    if not artist or not album:
        return ProcessResult(False, "Tags de artista/album nao encontradas", True)

    covered_files = [record.path for record in group_records if record.has_cover]
    files_missing_cover = [record.path for record in group_records if not record.has_cover]
    if not files_missing_cover:
        return ProcessResult(True, "Todas as musicas ja possuem capa", True)

    logger.info("[%s] Album identificado: %s - %s", dir_path, artist, album)
    logger.info(
        "[%s] %s musicas com capa, %s sem capa.",
        dir_path,
        len(covered_files),
        len(files_missing_cover),
    )

    cover_url, cover_error = fetch_cover_url(artist, album)
    if cover_error:
        is_transient = cover_error.startswith("Erro de rede") or cover_error.startswith(
            "Resposta invalida"
        )
        return ProcessResult(False, cover_error, not is_transient)

    logger.info("[%s] Baixando capa...", dir_path)
    image_data, download_error = download_cover(cover_url)
    if download_error:
        return ProcessResult(False, download_error, False)

    cover_path = os.path.join(dir_path, "cover.jpg")
    try:
        with open(cover_path, "wb") as file_obj:
            file_obj.write(image_data)
    except OSError as exc:
        return ProcessResult(False, f"Erro ao salvar cover.jpg: {exc}", False)

    failed_files = []
    for mp3_path in files_missing_cover:
        success, error_message = embed_cover(mp3_path, image_data)
        if not success:
            failed_files.append((mp3_path, error_message))

    if failed_files:
        logger.warning("[%s] Falhas ao embutir capa:", dir_path)
        for failed_path, error_message in failed_files:
            logger.warning("  - %s: %s", failed_path, error_message)
        return ProcessResult(
            False,
            f"Capa salva, mas {len(failed_files)} arquivo(s) falharam ao embutir",
            False,
        )

    return ProcessResult(True, "Capa adicionada com sucesso", True)


def run_cover_mode(args, music_dir, progress_file):
    progress = load_progress(progress_file)
    total_processed = 0

    logger.info("Iniciando varredura em %s...", music_dir)
    logger.info("Arquivo de progresso: %s", progress_file)

    for root, _, files in os.walk(music_dir):
        if root in progress:
            continue

        if any(file_name.lower().endswith(".mp3") for file_name in files):
            result = process_directory(root)
            logger.info("-> %s\n", result.message)

            if result.should_record_progress:
                progress.add(root)
                save_progress(progress_file, progress)

            total_processed += 1
            if args.delay > 0:
                time.sleep(args.delay)

    logger.info("Processamento concluido. %s novas pastas analisadas.", total_processed)
    return 0


def main():
    args = parse_args()
    music_dir = os.path.abspath(args.music_dir)
    progress_file = os.path.abspath(args.progress_file)
    configure_logging(args)

    if not os.path.exists(music_dir):
        logger.error("Erro: o diretorio %s nao existe.", music_dir)
        return 1

    if args.mode == "audit":
        return run_audit(args, music_dir)
    if args.mode == "fix-tags":
        return run_fix_tags(music_dir)
    if args.mode == "fix-covers":
        return run_fix_covers(music_dir)

    return run_cover_mode(args, music_dir, progress_file)


if __name__ == "__main__":
    raise SystemExit(main())
