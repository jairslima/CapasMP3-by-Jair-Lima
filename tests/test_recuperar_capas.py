import json
import logging
import os
import shutil
import unittest
from unittest.mock import mock_open, patch

import recuperar_capas
from mutagen.id3 import APIC, ID3, TALB, TPE1

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def read_fixture_text(file_name):
    with open(os.path.join(FIXTURES_DIR, file_name), "r", encoding="utf-8") as file_obj:
        return file_obj.read()


def read_fixture_bytes(file_name):
    with open(os.path.join(FIXTURES_DIR, file_name), "rb") as file_obj:
        return file_obj.read()


class WorkspaceDirTestCase(unittest.TestCase):
    def setUp(self):
        runtime_root = os.path.join(os.getcwd(), "tests_runtime")
        os.makedirs(runtime_root, exist_ok=True)
        self.temp_dir = os.path.join(runtime_root, self._testMethodName)
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        os.makedirs(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class ProgressTests(unittest.TestCase):
    @patch("recuperar_capas.os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=mock_open, read_data="{invalido")
    def test_load_progress_returns_empty_set_for_invalid_json(self, _open_mock, _exists_mock):
        with self.assertLogs("recuperar_capas", level="WARNING") as captured:
            progress = recuperar_capas.load_progress("progresso.json")

        self.assertEqual(progress, set())
        self.assertIn("Progresso invalido", "\n".join(captured.output))

    @patch("builtins.open", new_callable=mock_open)
    def test_save_and_load_progress_round_trip(self, _open_patch):
        expected = {r"C:\Music\Album B", r"C:\Music\Album A"}
        mock_file = mock_open()

        with patch("builtins.open", mock_file):
            recuperar_capas.save_progress("progresso.json", expected)

        written = "".join(call.args[0] for call in mock_file().write.call_args_list)
        self.assertEqual(json.loads(written), sorted(expected))

        with patch("recuperar_capas.os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=written)):
                loaded = recuperar_capas.load_progress("progresso.json")

        self.assertEqual(loaded, expected)


class LoggingConfigTests(unittest.TestCase):
    def test_configure_logging_defaults_to_info(self):
        args = unittest.mock.Mock(quiet=False, verbose=False)

        with patch("recuperar_capas.logging.basicConfig") as basic_config_mock:
            recuperar_capas.configure_logging(args)

        basic_config_mock.assert_called_once_with(level=logging.INFO, format="%(message)s")

    def test_configure_logging_uses_error_for_quiet(self):
        args = unittest.mock.Mock(quiet=True, verbose=False)

        with patch("recuperar_capas.logging.basicConfig") as basic_config_mock:
            recuperar_capas.configure_logging(args)

        basic_config_mock.assert_called_once_with(level=logging.ERROR, format="%(message)s")

    def test_configure_logging_gives_quiet_precedence_over_verbose(self):
        args = unittest.mock.Mock(quiet=True, verbose=True)

        with patch("recuperar_capas.logging.basicConfig") as basic_config_mock:
            recuperar_capas.configure_logging(args)

        basic_config_mock.assert_called_once_with(level=logging.ERROR, format="%(message)s")


class FilenameParsingTests(unittest.TestCase):
    def test_parse_filename_metadata_extracts_expected_fields(self):
        inferred = recuperar_capas.parse_filename_metadata(
            "Actos 2 - Mundo Perdido - 06 - Ele Veio.mp3"
        )

        self.assertEqual(inferred.artist, "Actos 2")
        self.assertEqual(inferred.album, "Mundo Perdido")
        self.assertEqual(inferred.track_number, "06")
        self.assertEqual(inferred.title, "Ele Veio")
        self.assertEqual(inferred.confidence, "high")

    def test_parse_filename_metadata_returns_empty_when_pattern_is_incompatible(self):
        inferred = recuperar_capas.parse_filename_metadata("Faixa solta qualquer.mp3")

        self.assertIsNone(inferred.artist)
        self.assertEqual(inferred.confidence, "none")


class AuditGroupingTests(unittest.TestCase):
    def test_detect_mixed_folders_finds_multiple_groups_in_same_folder(self):
        folder = r"C:\Music\Actos 2++"
        records = [
            recuperar_capas.TrackRecord(
                path=os.path.join(folder, "a.mp3"),
                folder=folder,
                filename="a.mp3",
                artist_tag="Actos 2",
                album_tag="Cenaculo",
                group_key="actos 2::cenaculo",
            ),
            recuperar_capas.TrackRecord(
                path=os.path.join(folder, "b.mp3"),
                folder=folder,
                filename="b.mp3",
                artist_tag="Actos 2",
                album_tag="Mundo Perdido",
                group_key="actos 2::mundo perdido",
            ),
        ]

        mixed = recuperar_capas.detect_mixed_folders(records)

        self.assertEqual(
            mixed,
            {folder: ["actos 2::cenaculo", "actos 2::mundo perdido"]},
        )


class DirectoryClassificationTests(unittest.TestCase):
    def test_classify_directory_splits_files_with_and_without_cover(self):
        mp3_files = ["a.mp3", "b.mp3", "c.mp3"]

        with patch(
            "recuperar_capas.has_embedded_cover",
            side_effect=lambda path: path != "b.mp3",
        ):
            covered, missing = recuperar_capas.classify_directory(mp3_files)

        self.assertEqual(covered, ["a.mp3", "c.mp3"])
        self.assertEqual(missing, ["b.mp3"])


class FakeAudioFile:
    def __init__(self, tags=None):
        self.tags = tags
        self.saved = False
        self.added_tags = False

    def add_tags(self):
        self.tags = ID3()
        self.added_tags = True

    def save(self):
        self.saved = True


class MutagenContractTests(unittest.TestCase):
    def test_get_album_info_reads_artist_and_album_from_tags(self):
        tags = ID3()
        tags.add(TALB(encoding=3, text="Album Real"))
        tags.add(TPE1(encoding=3, text="Artista Real"))
        fake_audio = FakeAudioFile(tags=tags)

        with patch("recuperar_capas.MP3", return_value=fake_audio):
            artist, album = recuperar_capas.get_album_info("faixa.mp3")

        self.assertEqual(artist, "Artista Real")
        self.assertEqual(album, "Album Real")

    def test_embed_cover_replaces_existing_apic_and_saves(self):
        tags = ID3()
        tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="OldCover",
                data=b"old-image",
            )
        )
        fake_audio = FakeAudioFile(tags=tags)

        with patch("recuperar_capas.MP3", return_value=fake_audio):
            success, error = recuperar_capas.embed_cover("faixa.mp3", b"new-image")

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertTrue(fake_audio.saved)
        apic_frames = [frame for frame in fake_audio.tags.values() if isinstance(frame, APIC)]
        self.assertEqual(len(apic_frames), 1)
        self.assertEqual(apic_frames[0].data, b"new-image")

    def test_embed_cover_adds_tags_when_missing(self):
        fake_audio = FakeAudioFile(tags=None)

        with patch("recuperar_capas.MP3", return_value=fake_audio):
            success, error = recuperar_capas.embed_cover("faixa.mp3", b"image-data")

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertTrue(fake_audio.added_tags)
        self.assertTrue(fake_audio.saved)

    def test_apply_inferred_tags_writes_only_missing_fields(self):
        tags = ID3()
        tags.add(TPE1(encoding=3, text="Artista Existente"))
        fake_audio = FakeAudioFile(tags=tags)
        inferred = recuperar_capas.InferredMetadata(
            artist="Actos 2",
            album="Mundo Perdido",
            track_number="06",
            title="Ele Veio",
            confidence="high",
            source="filename",
        )

        with patch("recuperar_capas.MP3", return_value=fake_audio):
            success, updated_fields = recuperar_capas.apply_inferred_tags("faixa.mp3", inferred)

        self.assertTrue(success)
        self.assertEqual(updated_fields, ["album", "track_number", "title"])
        self.assertEqual(fake_audio.tags.get("TPE1").text[0], "Artista Existente")
        self.assertEqual(fake_audio.tags.get("TALB").text[0], "Mundo Perdido")
        self.assertEqual(fake_audio.tags.get("TRCK").text[0], "06")
        self.assertEqual(fake_audio.tags.get("TIT2").text[0], "Ele Veio")


class FixTagsTests(unittest.TestCase):
    def test_get_missing_tag_fields_uses_track_tag_mapping(self):
        record = recuperar_capas.TrackRecord(
            path="faixa.mp3",
            folder="album",
            filename="faixa.mp3",
            artist_tag="Actos 2",
            album_tag="Cenaculo",
            title_tag="Atos 2",
            track_tag="01",
        )

        self.assertEqual(recuperar_capas.get_missing_tag_fields(record), [])

    def test_can_write_inferred_tags_requires_high_confidence_and_missing_fields(self):
        record = recuperar_capas.TrackRecord(
            path="faixa.mp3",
            folder="album",
            filename="faixa.mp3",
            artist_tag=None,
            album_tag=None,
            artist_inferred="Actos 2",
            album_inferred="Cenaculo",
            track_inferred="01",
            title_inferred="Atos 2",
            inference_confidence="high",
        )

        self.assertTrue(recuperar_capas.can_write_inferred_tags(record))

    @patch(
        "recuperar_capas.scan_library",
        return_value=(
            [
                recuperar_capas.TrackRecord(
                    path="a.mp3",
                    folder="album",
                    filename="a.mp3",
                    artist_tag=None,
                    album_tag=None,
                    title_tag=None,
                    track_tag=None,
                    artist_inferred="Actos 2",
                    album_inferred="Mundo Perdido",
                    title_inferred="Ele Veio",
                    track_inferred="06",
                    inference_confidence="high",
                    inference_source="filename",
                ),
                recuperar_capas.TrackRecord(
                    path="b.mp3",
                    folder="album",
                    filename="b.mp3",
                    artist_tag=None,
                    album_tag=None,
                    title_tag=None,
                    track_tag=None,
                    artist_inferred=None,
                    album_inferred=None,
                    title_inferred=None,
                    track_inferred=None,
                    inference_confidence="none",
                    inference_source="none",
                ),
            ],
            {"album"},
        ),
    )
    @patch("recuperar_capas.apply_inferred_tags", return_value=(True, ["artist", "album", "track_number", "title"]))
    def test_run_fix_tags_updates_only_high_confidence_records(
        self, apply_inferred_tags_mock, _scan_library
    ):
        exit_code = recuperar_capas.run_fix_tags("music-dir")

        self.assertEqual(exit_code, 0)
        apply_inferred_tags_mock.assert_called_once()


class FixCoversTests(unittest.TestCase):
    def test_build_cover_search_candidates_includes_artist_fallbacks(self):
        record = recuperar_capas.TrackRecord(
            path="a.mp3",
            folder="album",
            filename="a.mp3",
            artist_tag="Grupo Actos 2",
            album_tag="Jesus esta aqui",
            artist_inferred="Actos 2",
            album_inferred="Jesus esta aqui",
        )

        candidates = recuperar_capas.build_cover_search_candidates(record)

        self.assertIn(("Grupo Actos 2", "Jesus esta aqui"), candidates)
        self.assertIn(("Actos 2", "Jesus esta aqui"), candidates)
        self.assertIn(("", "Jesus esta aqui"), candidates)

    @patch("recuperar_capas.save_group_cover_file")
    @patch("recuperar_capas.embed_cover", return_value=(True, None))
    @patch("recuperar_capas.download_cover", return_value=(b"new-image", None))
    @patch("recuperar_capas.fetch_cover_url", return_value=("https://img.test/cover.jpg", None))
    def test_fix_group_covers_updates_only_divergent_tracks(
        self,
        _fetch_cover_url,
        _download_cover,
        embed_cover_mock,
        _save_group_cover_file,
    ):
        expected_fingerprint = recuperar_capas.compute_image_fingerprint(b"new-image")
        records = [
            recuperar_capas.TrackRecord(
                path="a.mp3",
                folder="album",
                filename="a.mp3",
                artist_tag="Actos 2",
                album_tag="Mundo Perdido",
                has_cover=True,
                cover_fingerprint=expected_fingerprint,
                group_key="actos 2::mundo perdido",
            ),
            recuperar_capas.TrackRecord(
                path="b.mp3",
                folder="album",
                filename="b.mp3",
                artist_tag="Actos 2",
                album_tag="Mundo Perdido",
                has_cover=True,
                cover_fingerprint="oldfinger123",
                group_key="actos 2::mundo perdido",
            ),
        ]

        success, error, updated_files = recuperar_capas.fix_group_covers(
            "actos 2::mundo perdido",
            records,
        )

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertEqual(updated_files, ["b.mp3"])
        embed_cover_mock.assert_called_once_with("b.mp3", b"new-image")

    @patch("recuperar_capas.save_group_cover_file")
    @patch("recuperar_capas.embed_cover", return_value=(True, None))
    @patch("recuperar_capas.download_cover", return_value=(b"new-image", None))
    @patch(
        "recuperar_capas.fetch_cover_url",
        side_effect=[
            (None, "Capa nao encontrada no iTunes"),
            ("https://img.test/cover.jpg", None),
        ],
    )
    def test_fix_group_covers_uses_fallback_search_candidates(
        self,
        fetch_cover_url_mock,
        _download_cover,
        embed_cover_mock,
        _save_group_cover_file,
    ):
        records = [
            recuperar_capas.TrackRecord(
                path="a.mp3",
                folder="album",
                filename="a.mp3",
                artist_tag="Grupo Actos 2",
                album_tag="Jesus esta aqui",
                artist_inferred="Actos 2",
                album_inferred="Jesus esta aqui",
                has_cover=False,
                cover_fingerprint=None,
                group_key="grupo actos 2::jesus esta aqui",
            ),
        ]

        success, error, updated_files = recuperar_capas.fix_group_covers(
            "grupo actos 2::jesus esta aqui",
            records,
        )

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertEqual(updated_files, ["a.mp3"])
        self.assertEqual(fetch_cover_url_mock.call_count, 2)
        embed_cover_mock.assert_called_once_with("a.mp3", b"new-image")

    @patch(
        "recuperar_capas.scan_library",
        return_value=(
            [
                recuperar_capas.TrackRecord(
                    path="a.mp3",
                    folder="album",
                    filename="a.mp3",
                    artist_tag="Actos 2",
                    album_tag="Cenaculo",
                    group_key="actos 2::cenaculo",
                ),
                recuperar_capas.TrackRecord(
                    path="b.mp3",
                    folder="album",
                    filename="b.mp3",
                    artist_tag=None,
                    album_tag=None,
                    group_key="desconhecido::sem-album",
                ),
            ],
            {"album"},
        ),
    )
    @patch(
        "recuperar_capas.fix_group_covers",
        return_value=(True, None, ["a.mp3"]),
    )
    def test_run_fix_covers_skips_groups_without_resolved_album(
        self, fix_group_covers_mock, _scan_library
    ):
        exit_code = recuperar_capas.run_fix_covers("music-dir")

        self.assertEqual(exit_code, 0)
        fix_group_covers_mock.assert_called_once()


class ProcessDirectoryTests(unittest.TestCase):
    @patch("recuperar_capas.glob.glob", return_value=["a.mp3", "b.mp3"])
    @patch(
        "recuperar_capas.read_track_metadata",
        side_effect=[
            recuperar_capas.TrackRecord(
                path="a.mp3",
                folder="album-dir",
                filename="a.mp3",
                artist_tag="Actos 2",
                album_tag="Cenaculo",
                has_cover=False,
                group_key="actos 2::cenaculo",
            ),
            recuperar_capas.TrackRecord(
                path="b.mp3",
                folder="album-dir",
                filename="b.mp3",
                artist_tag="Actos 2",
                album_tag="Mundo Perdido",
                has_cover=False,
                group_key="actos 2::mundo perdido",
            ),
        ],
    )
    def test_process_directory_refuses_mixed_album_folder(self, _read_track_metadata, _glob):
        result = recuperar_capas.process_directory("album-dir")

        self.assertFalse(result.success)
        self.assertIn("multiplos albuns logicos", result.message)

    @patch("recuperar_capas.glob.glob", return_value=["a.mp3", "b.mp3"])
    @patch(
        "recuperar_capas.read_track_metadata",
        side_effect=[
            recuperar_capas.TrackRecord(
                path="a.mp3",
                folder="album",
                filename="a.mp3",
                artist_tag="Artista",
                album_tag="Album",
                has_cover=True,
                group_key="artista::album",
            ),
            recuperar_capas.TrackRecord(
                path="b.mp3",
                folder="album",
                filename="b.mp3",
                artist_tag="Artista",
                album_tag="Album",
                has_cover=False,
                group_key="artista::album",
            ),
        ],
    )
    @patch("recuperar_capas.fetch_cover_url", return_value=(None, "Erro de rede ao consultar iTunes: timeout"))
    def test_process_directory_keeps_transient_itunes_failure_out_of_progress(
        self, _fetch_cover_url, _read_track_metadata, _glob
    ):
        result = recuperar_capas.process_directory("album")

        self.assertFalse(result.success)
        self.assertFalse(result.should_record_progress)
        self.assertIn("Erro de rede", result.message)

    @patch("recuperar_capas.glob.glob", return_value=["a.mp3", "b.mp3"])
    @patch(
        "recuperar_capas.read_track_metadata",
        side_effect=[
            recuperar_capas.TrackRecord(
                path="a.mp3",
                folder="album",
                filename="a.mp3",
                artist_tag="Artista",
                album_tag="Album",
                has_cover=True,
                group_key="artista::album",
            ),
            recuperar_capas.TrackRecord(
                path="b.mp3",
                folder="album",
                filename="b.mp3",
                artist_tag="Artista",
                album_tag="Album",
                has_cover=False,
                group_key="artista::album",
            ),
        ],
    )
    @patch("recuperar_capas.fetch_cover_url", return_value=(None, "Capa nao encontrada no iTunes"))
    def test_process_directory_records_permanent_miss_in_progress(
        self, _fetch_cover_url, _read_track_metadata, _glob
    ):
        result = recuperar_capas.process_directory("album")

        self.assertFalse(result.success)
        self.assertTrue(result.should_record_progress)
        self.assertEqual(result.message, "Capa nao encontrada no iTunes")

    @patch("recuperar_capas.glob.glob", return_value=["a.mp3", "b.mp3"])
    @patch(
        "recuperar_capas.read_track_metadata",
        side_effect=[
            recuperar_capas.TrackRecord(
                path="a.mp3",
                folder="album-dir",
                filename="a.mp3",
                artist_tag="Artista",
                album_tag="Album",
                has_cover=True,
                group_key="artista::album",
            ),
            recuperar_capas.TrackRecord(
                path="b.mp3",
                folder="album-dir",
                filename="b.mp3",
                artist_tag="Artista",
                album_tag="Album",
                has_cover=False,
                group_key="artista::album",
            ),
        ],
    )
    @patch("recuperar_capas.fetch_cover_url", return_value=("https://img.test/cover.jpg", None))
    @patch("recuperar_capas.download_cover", return_value=(b"image-bytes", None))
    @patch("recuperar_capas.embed_cover", return_value=(True, None))
    @patch("builtins.open", new_callable=mock_open)
    def test_process_directory_embeds_only_missing_files(
        self,
        open_mock,
        embed_cover_mock,
        _download_cover,
        _fetch_cover_url,
        _read_track_metadata,
        _glob,
    ):
        result = recuperar_capas.process_directory("album-dir")

        self.assertTrue(result.success)
        self.assertTrue(result.should_record_progress)
        self.assertEqual(result.message, "Capa adicionada com sucesso")
        embed_cover_mock.assert_called_once_with("b.mp3", b"image-bytes")
        open_mock.assert_called_once_with(os.path.join("album-dir", "cover.jpg"), "wb")

    @patch("recuperar_capas.glob.glob", return_value=["a.mp3", "b.mp3"])
    @patch(
        "recuperar_capas.read_track_metadata",
        side_effect=[
            recuperar_capas.TrackRecord(
                path="a.mp3",
                folder="album-dir",
                filename="a.mp3",
                artist_tag="Artista",
                album_tag="Album",
                has_cover=False,
                group_key="artista::album",
            ),
            recuperar_capas.TrackRecord(
                path="b.mp3",
                folder="album-dir",
                filename="b.mp3",
                artist_tag="Artista",
                album_tag="Album",
                has_cover=False,
                group_key="artista::album",
            ),
        ],
    )
    @patch("recuperar_capas.fetch_cover_url", return_value=("https://img.test/cover.jpg", None))
    @patch("recuperar_capas.download_cover", return_value=(b"image-bytes", None))
    @patch(
        "recuperar_capas.embed_cover",
        side_effect=[(True, None), (False, "arquivo bloqueado")],
    )
    @patch("builtins.open", new_callable=mock_open)
    def test_process_directory_does_not_record_partial_embed_failure(
        self,
        _open_mock,
        _embed_cover,
        _download_cover,
        _fetch_cover_url,
        _read_track_metadata,
        _glob,
    ):
        result = recuperar_capas.process_directory("album-dir")

        self.assertFalse(result.success)
        self.assertFalse(result.should_record_progress)
        self.assertIn("1 arquivo(s) falharam", result.message)


class NetworkIntegrationTests(unittest.TestCase):
    def test_fetch_cover_url_uses_local_fixture_payload(self):
        fixture_payload = json.loads(read_fixture_text("itunes_album_search.json"))

        mock_response = unittest.mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = fixture_payload

        with patch("recuperar_capas.requests.get", return_value=mock_response) as get_mock:
            cover_url, error = recuperar_capas.fetch_cover_url("Artista Fixture", "Album Fixture")

        self.assertIsNone(error)
        self.assertEqual(
            cover_url,
            "https://is1-ssl.mzstatic.com/image/thumb/Music126/v4/example/600x600bb.jpg",
        )
        get_mock.assert_called_once()

    def test_fetch_cover_url_sends_country_br(self):
        fixture_payload = json.loads(read_fixture_text("itunes_album_search.json"))
        mock_response = unittest.mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = fixture_payload

        with patch("recuperar_capas.requests.get", return_value=mock_response) as get_mock:
            recuperar_capas.fetch_cover_url("Actos 2", "Original")

        _, kwargs = get_mock.call_args
        params = kwargs.get("params", {})
        self.assertEqual(params.get("country"), "br")

    def test_fetch_cover_url_deezer_returns_cover_big(self):
        mock_response = unittest.mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "data": [
                {
                    "title": "O Fim dos Tempos",
                    "artist": {"name": "Actos 2"},
                    "cover_big": "https://cdn-images.dzcdn.net/images/cover/abc123/500x500-000000-80-0-0.jpg",
                }
            ]
        }

        with patch("recuperar_capas.requests.get", return_value=mock_response):
            cover_url, error = recuperar_capas.fetch_cover_url_deezer("Actos 2", "O Fim dos Tempos")

        self.assertIsNone(error)
        self.assertEqual(cover_url, "https://cdn-images.dzcdn.net/images/cover/abc123/500x500-000000-80-0-0.jpg")

    def test_fetch_cover_url_deezer_returns_error_when_empty(self):
        mock_response = unittest.mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": []}

        with patch("recuperar_capas.requests.get", return_value=mock_response):
            cover_url, error = recuperar_capas.fetch_cover_url_deezer("Actos 2", "Jesus esta aqui")

        self.assertIsNone(cover_url)
        self.assertIn("Deezer", error)

    def test_fix_group_covers_falls_back_to_deezer_when_itunes_fails(self):
        records = [
            recuperar_capas.TrackRecord(
                path="a.mp3",
                folder="album",
                filename="a.mp3",
                artist_tag="Actos 2",
                album_tag="O Fim dos Tempos",
                has_cover=True,
                cover_fingerprint="oldfinger123",
                group_key="actos 2::o fim dos tempos",
            )
        ]

        with patch("recuperar_capas.fetch_cover_url", return_value=(None, "Capa nao encontrada no iTunes")):
            with patch("recuperar_capas.fetch_cover_url_deezer", return_value=("https://cdn.deezer.com/cover.jpg", None)) as deezer_mock:
                with patch("recuperar_capas.download_cover", return_value=(b"deezer-image", None)):
                    with patch("recuperar_capas.embed_cover", return_value=(True, None)):
                        with patch("recuperar_capas.save_group_cover_file"):
                            success, error, updated = recuperar_capas.fix_group_covers(
                                "actos 2::o fim dos tempos", records
                            )

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertEqual(updated, ["a.mp3"])
        deezer_mock.assert_called()

    def test_download_cover_returns_fixture_bytes(self):
        fixture_bytes = read_fixture_bytes("cover_bytes.bin")

        mock_response = unittest.mock.Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = fixture_bytes

        with patch("recuperar_capas.requests.get", return_value=mock_response):
            image_data, error = recuperar_capas.download_cover("https://img.test/cover.jpg")

        self.assertIsNone(error)
        self.assertEqual(image_data, fixture_bytes)


class MainIntegrationTests(WorkspaceDirTestCase):
    @patch("recuperar_capas.time.sleep")
    def test_main_persists_only_recordable_directories(self, _sleep_mock):
        album_a = os.path.join(self.temp_dir, "AlbumA")
        album_b = os.path.join(self.temp_dir, "AlbumB")
        os.makedirs(album_a)
        os.makedirs(album_b)
        for path in (
            os.path.join(album_a, "faixa1.mp3"),
            os.path.join(album_b, "faixa1.mp3"),
        ):
            with open(path, "wb") as file_obj:
                file_obj.write(b"")

        progress_file = os.path.join(self.temp_dir, "progresso.json")

        results = [
            recuperar_capas.ProcessResult(True, "ok", True),
            recuperar_capas.ProcessResult(False, "Erro de rede ao baixar capa", False),
        ]

        with patch("recuperar_capas.process_directory", side_effect=results) as process_mock:
            with patch(
                "sys.argv",
                [
                    "recuperar_capas.py",
                    "--music-dir",
                    self.temp_dir,
                    "--progress-file",
                    progress_file,
                    "--delay",
                    "0",
                ],
            ):
                exit_code = recuperar_capas.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(process_mock.call_count, 2)
        with open(progress_file, "r", encoding="utf-8") as file_obj:
            progress = json.load(file_obj)
        self.assertEqual(progress, [album_a])

    def test_main_audit_generates_reports_for_mixed_folder(self):
        folder = os.path.join(self.temp_dir, "Actos 2++")
        os.makedirs(folder)
        audit_md_file = os.path.join(self.temp_dir, "audit.md")
        audit_json_file = os.path.join(self.temp_dir, "audit.json")

        fake_records = [
            recuperar_capas.TrackRecord(
                path=os.path.join(folder, "a.mp3"),
                folder=folder,
                filename="a.mp3",
                artist_tag="Actos 2",
                album_tag="Cenaculo",
                has_cover=False,
                group_key="actos 2::cenaculo",
                issues=["sem-capa"],
            ),
            recuperar_capas.TrackRecord(
                path=os.path.join(folder, "b.mp3"),
                folder=folder,
                filename="b.mp3",
                artist_tag=None,
                album_tag=None,
                artist_inferred="Actos 2",
                album_inferred="Mundo Perdido",
                has_cover=False,
                group_key="actos 2::mundo perdido",
                issues=["tags-ausentes", "metadados-inferidos", "sem-capa"],
            ),
        ]

        with patch(
            "recuperar_capas.scan_library",
            return_value=(fake_records, {folder}),
        ):
            with patch(
                "sys.argv",
                [
                    "recuperar_capas.py",
                    "--mode",
                    "audit",
                    "--music-dir",
                    self.temp_dir,
                    "--audit-md-file",
                    audit_md_file,
                    "--audit-json-file",
                    audit_json_file,
                ],
            ):
                exit_code = recuperar_capas.main()

        self.assertEqual(exit_code, 0)
        with open(audit_md_file, "r", encoding="utf-8") as file_obj:
            markdown = file_obj.read()
        with open(audit_json_file, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)

        self.assertIn("Pastas com multiplos albuns: 1", markdown)
        self.assertIn("actos 2::mundo perdido", markdown)
        self.assertEqual(payload["summary"]["mixed_folders"], 1)


class CleanupStaleArtTests(WorkspaceDirTestCase):
    def test_cleanup_removes_albumart_folder_and_cover_files(self):
        stale_files = [
            "AlbumArt_{GUID-1234}_Large.jpg",
            "AlbumArt_{GUID-1234}_Small.jpg",
            "AlbumArtSmall.jpg",
            "Folder.jpg",
            "cover.jpg",
        ]
        kept_files = [
            "cover - Actos 2 - Cenaculo.jpg",
            "cover - Actos 2 - Mundo Perdido.jpg",
        ]
        for fname in stale_files + kept_files:
            with open(os.path.join(self.temp_dir, fname), "wb") as f:
                f.write(b"fake")

        removed = recuperar_capas.cleanup_stale_folder_art(self.temp_dir)

        self.assertEqual(sorted(removed), sorted(stale_files))
        for fname in kept_files:
            self.assertTrue(os.path.exists(os.path.join(self.temp_dir, fname)))
        for fname in stale_files:
            self.assertFalse(os.path.exists(os.path.join(self.temp_dir, fname)))

    def test_cleanup_returns_empty_list_when_nothing_to_remove(self):
        removed = recuperar_capas.cleanup_stale_folder_art(self.temp_dir)
        self.assertEqual(removed, [])

    def test_run_fix_covers_cleans_stale_art_after_updating_files(self):
        folder = self.temp_dir
        stale = os.path.join(folder, "Folder.jpg")
        with open(stale, "wb") as f:
            f.write(b"old-art")

        records = [
            recuperar_capas.TrackRecord(
                path=os.path.join(folder, "a.mp3"),
                folder=folder,
                filename="a.mp3",
                artist_tag="Actos 2",
                album_tag="Cenaculo",
                group_key="actos 2::cenaculo",
                cover_fingerprint="oldfinger",
            )
        ]

        with patch("recuperar_capas.scan_library", return_value=(records, {folder})):
            with patch("recuperar_capas.fix_group_covers", return_value=(True, None, [os.path.join(folder, "a.mp3")])):
                with patch("recuperar_capas.notify_shell_dir_updated"):
                    recuperar_capas.run_fix_covers(folder)

        self.assertFalse(os.path.exists(stale))


class ShellNotificationTests(unittest.TestCase):
    def test_embed_cover_notifies_shell_after_saving(self):
        fake_audio = FakeAudioFile(tags=None)

        with patch("recuperar_capas.MP3", return_value=fake_audio):
            with patch("recuperar_capas.notify_shell_file_updated") as notify_mock:
                success, error = recuperar_capas.embed_cover("faixa.mp3", b"image-data")

        self.assertTrue(success)
        notify_mock.assert_called_once_with("faixa.mp3")

    def test_embed_cover_does_not_notify_shell_on_failure(self):
        with patch("recuperar_capas.MP3", side_effect=Exception("arquivo bloqueado")):
            with patch("recuperar_capas.notify_shell_file_updated") as notify_mock:
                success, error = recuperar_capas.embed_cover("faixa.mp3", b"image-data")

        self.assertFalse(success)
        notify_mock.assert_not_called()

    def test_notify_shell_file_updated_skips_silently_on_non_windows(self):
        with patch("recuperar_capas.sys.platform", "linux"):
            recuperar_capas.notify_shell_file_updated("faixa.mp3")

    def test_notify_shell_file_updated_calls_SHChangeNotify_on_windows(self):
        mock_shell32 = unittest.mock.Mock()
        mock_windll = unittest.mock.Mock(shell32=mock_shell32)
        mock_ctypes = unittest.mock.Mock(windll=mock_windll)
        mock_ctypes.c_wchar_p = str

        with patch("recuperar_capas.sys.platform", "win32"):
            with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
                with patch("recuperar_capas.os.path.abspath", return_value="C:\\Music\\faixa.mp3"):
                    import importlib
                    import ctypes as real_ctypes
                    with patch("recuperar_capas.sys.platform", "win32"):
                        with patch("ctypes.windll") as windll_mock:
                            windll_mock.shell32.SHChangeNotify = unittest.mock.Mock()
                            recuperar_capas.notify_shell_file_updated("faixa.mp3")
                            windll_mock.shell32.SHChangeNotify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
