"""The audio lab — probing, the Godot loop settings, and safe writes.

The DSP is in the browser, so what is pinned here is everything the browser
cannot check for itself. The loop tests carry most of the weight: a music track
whose ``.import`` says ``loop=false`` plays once and stops, nothing about the
audio reveals it, and the wav and ogg importers spell the same idea in two
completely different vocabularies. Getting one of those spellings wrong
produces a file that looks saved and behaves wrong in the game.
"""
from __future__ import annotations

import base64
import io
import json
import math
import struct
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bgate_core import audiolab
from bgate_ui.app import app


# ---------------------------------------------------------------------------
# Fixtures — real WAV bytes, and a hand-built Ogg page stream
# ---------------------------------------------------------------------------
def _wav_bytes(seconds: float = 0.25, rate: int = 44100, channels: int = 1,
               freq: float = 440.0) -> bytes:
    frames = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            v = int(0.4 * 32767 * math.sin(2 * math.pi * freq * i / rate))
            for _ in range(channels):
                data += struct.pack("<h", v)
        w.writeframes(bytes(data))
    return buf.getvalue()


def _ogg_page(granule: int, seq: int, flags: int, payload: bytes) -> bytes:
    segments = [len(payload)] if len(payload) < 255 else [255, len(payload) - 255]
    return (b"OggS" + bytes([0, flags]) + struct.pack("<q", granule)
            + struct.pack("<I", 1) + struct.pack("<I", seq) + b"\0\0\0\0"
            + bytes([len(segments)]) + bytes(segments) + payload)


def _ogg_bytes(rate: int = 44100, channels: int = 2, frames: int = 88200) -> bytes:
    ident = (b"\x01vorbis" + struct.pack("<I", 0) + bytes([channels])
             + struct.pack("<I", rate) + b"\0" * 12)
    return (_ogg_page(0, 0, 2, ident)
            + _ogg_page(frames // 2, 1, 0, b"\x05" * 64)
            + _ogg_page(frames, 2, 4, b"\x05" * 32))


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def test_a_wav_reports_its_real_shape(tmp_path):
    p = tmp_path / "hit.wav"
    p.write_bytes(_wav_bytes(seconds=0.5, rate=22050, channels=2))
    info = audiolab.probe(p)
    assert info["sample_rate"] == 22050
    assert info["channels"] == 2
    assert info["seconds"] == pytest.approx(0.5, abs=0.001)
    assert info["probe_error"] is None


def test_an_ogg_length_comes_from_the_last_granule(tmp_path):
    """Duration lives in the FINAL page, so a 6 MB track costs two short reads."""
    p = tmp_path / "music.ogg"
    p.write_bytes(_ogg_bytes(rate=44100, channels=2, frames=88200))
    info = audiolab.probe(p)
    assert info["sample_rate"] == 44100
    assert info["channels"] == 2
    assert info["seconds"] == pytest.approx(2.0, abs=0.001)


def test_an_unreadable_file_still_gets_a_row(tmp_path):
    """"Broken" must not mean "absent from the listing"."""
    p = tmp_path / "junk.wav"
    p.write_bytes(b"not a wav at all")
    info = audiolab.probe(p)
    assert info["seconds"] is None and info["probe_error"]


def test_mp3_is_not_guessed_at(tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"\xff\xfb" + b"\0" * 100)
    assert audiolab.probe(p)["seconds"] is None


# ---------------------------------------------------------------------------
# WAV validation — the gate before disk
# ---------------------------------------------------------------------------
def test_a_sane_wav_passes_and_reports_itself():
    info = audiolab.validate_wav(_wav_bytes(seconds=0.1))
    assert info["sample_rate"] == 44100 and info["channels"] == 1


def test_rubbish_is_refused_with_a_reason():
    with pytest.raises(audiolab.AudioError):
        audiolab.validate_wav(b"nope")


def test_an_absurd_channel_count_is_refused():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(6); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\0" * 240)
    with pytest.raises(audiolab.AudioError) as exc:
        audiolab.validate_wav(buf.getvalue())
    assert "channels" in str(exc.value)


# ---------------------------------------------------------------------------
# Godot loop settings — the setting you cannot hear
# ---------------------------------------------------------------------------
OGG_IMPORT = """[remap]

importer="oggvorbisstr"
type="AudioStreamOggVorbis"

[deps]

source_file="res://assets/audio/music.ogg"

[params]

loop=false
loop_offset=0
bpm=0
"""

WAV_IMPORT = """[remap]

importer="wav"
type="AudioStreamWAV"

[params]

edit/trim=false
edit/loop_mode=0
edit/loop_begin=0
edit/loop_end=-1
compress/mode=2
"""


@pytest.fixture()
def ogg(tmp_path):
    p = tmp_path / "music.ogg"
    p.write_bytes(_ogg_bytes(frames=44100 * 30))
    audiolab.import_path(p).write_text(OGG_IMPORT, encoding="utf-8")
    return p


@pytest.fixture()
def wav(tmp_path):
    p = tmp_path / "hit.wav"
    p.write_bytes(_wav_bytes(seconds=2.0))
    audiolab.import_path(p).write_text(WAV_IMPORT, encoding="utf-8")
    return p


def test_a_track_that_will_not_loop_says_so(ogg):
    state = audiolab.loop_state(ogg)
    assert state["supported"] is True and state["enabled"] is False
    assert state["importer"] == "ogg" and state["has_import"] is True


def test_enabling_an_ogg_loop_writes_the_ogg_importers_spelling(ogg):
    out = audiolab.write_loop(ogg, enabled=True, begin_s=4.5)
    text = audiolab.import_path(ogg).read_text(encoding="utf-8")
    assert "loop=true" in text and "loop_offset=4.5" in text
    assert out["loop"]["enabled"] is True
    assert out["loop"]["begin_s"] == pytest.approx(4.5)
    # Untouched keys survive — this edits a file the engine owns.
    assert "bpm=0" in text and 'importer="oggvorbisstr"' in text


def test_an_ogg_has_no_loop_end_and_says_when_one_was_offered(ogg):
    out = audiolab.write_loop(ogg, enabled=True, begin_s=1.0, end_s=9.0)
    assert out["ignored"], "silently dropping the loop end would be a lie"
    assert "loop end" in out["ignored"][0]


def test_a_wav_loop_is_written_in_FRAMES_not_seconds(wav):
    """The wav importer counts frames. Writing seconds there is a silent 44100x
    error that nothing reports and the game plays wrong."""
    audiolab.write_loop(wav, enabled=True, begin_s=0.5, end_s=1.5, mode="pingpong")
    text = audiolab.import_path(wav).read_text(encoding="utf-8")
    assert "edit/loop_begin=22050" in text
    assert "edit/loop_end=66150" in text
    assert "edit/loop_mode=2" in text
    state = audiolab.loop_state(wav)
    assert state["mode"] == "pingpong"
    assert state["begin_s"] == pytest.approx(0.5)
    assert state["end_s"] == pytest.approx(1.5)


def test_turning_looping_off_zeroes_the_mode_not_the_offsets(wav):
    audiolab.write_loop(wav, enabled=True, begin_s=0.5)
    audiolab.write_loop(wav, enabled=False, begin_s=0.5)
    assert audiolab.loop_state(wav)["enabled"] is False
    assert "edit/loop_mode=0" in audiolab.import_path(wav).read_text(encoding="utf-8")


def test_a_loop_past_the_end_of_the_clip_is_refused(wav):
    with pytest.raises(audiolab.AudioError) as exc:
        audiolab.write_loop(wav, enabled=True, begin_s=99.0)
    assert "past the clip" in str(exc.value)


def test_a_backwards_loop_range_is_refused(wav):
    with pytest.raises(audiolab.AudioError):
        audiolab.write_loop(wav, enabled=True, begin_s=1.0, end_s=0.5)


def test_a_missing_import_says_what_to_do(tmp_path):
    p = tmp_path / "fresh.wav"
    p.write_bytes(_wav_bytes())
    with pytest.raises(audiolab.AudioError) as exc:
        audiolab.write_loop(p, enabled=True)
    assert "Godot" in str(exc.value)


def test_a_param_missing_from_the_import_gets_added(tmp_path):
    p = tmp_path / "m.ogg"
    p.write_bytes(_ogg_bytes())
    audiolab.import_path(p).write_text(
        '[remap]\n\nimporter="oggvorbisstr"\n\n[params]\n\nbpm=0\n', encoding="utf-8")
    audiolab.write_loop(p, enabled=True, begin_s=2)
    text = audiolab.import_path(p).read_text(encoding="utf-8")
    assert "loop=true" in text and "bpm=0" in text


# ---------------------------------------------------------------------------
# Mix sessions
# ---------------------------------------------------------------------------
def test_a_session_round_trips(tmp_path):
    target = tmp_path / "mix.wav"
    saved = audiolab.save_session(target, {"tracks": [
        {"source": "assets/audio/sfx_hurt.wav", "offset_s": 0.25, "gain_db": -6}]})
    assert saved["updated_at"]
    assert audiolab.session_path(target).name == "mix.wav.mix.json"
    back = audiolab.load_session(target)
    assert back["tracks"][0]["source"] == "assets/audio/sfx_hurt.wav"
    assert back["tracks"][0]["offset_s"] == 0.25


def test_a_session_source_may_not_escape_the_project(tmp_path):
    for bad in ("../../etc/passwd", "/etc/passwd", "a/../../b.wav"):
        with pytest.raises(audiolab.AudioError):
            audiolab.normalise_session({"tracks": [{"source": bad}]})


def test_out_of_range_track_values_are_refused():
    with pytest.raises(audiolab.AudioError):
        audiolab.normalise_session({"tracks": [
            {"source": "a.wav", "gain_db": 400}]})
    with pytest.raises(audiolab.AudioError):
        audiolab.normalise_session({"tracks": [{"source": "a.wav", "pan": 9}]})


def test_a_session_round_trips_layer_trim_points(tmp_path):
    target = tmp_path / "mix.wav"
    audiolab.save_session(target, {"tracks": [
        {"source": "assets/audio/sfx_hurt.wav", "offset_s": 1.0,
         "in_s": 0.4, "out_s": 1.75}]})
    back = audiolab.load_session(target)
    assert back["tracks"][0]["in_s"] == 0.4
    assert back["tracks"][0]["out_s"] == 1.75


def test_a_track_without_trim_points_keeps_the_whole_source():
    out = audiolab.normalise_session({"tracks": [{"source": "a.wav"}]})
    assert out["tracks"][0]["in_s"] == 0.0
    assert out["tracks"][0]["out_s"] is None


def test_a_trim_that_ends_before_it_starts_is_refused():
    for bad in (0.5, 0.25):
        with pytest.raises(audiolab.AudioError):
            audiolab.normalise_session({"tracks": [
                {"source": "a.wav", "in_s": 0.5, "out_s": bad}]})


def test_a_trim_past_the_cap_is_refused():
    with pytest.raises(audiolab.AudioError):
        audiolab.normalise_session({"tracks": [
            {"source": "a.wav", "in_s": audiolab.MAX_SECONDS + 1}]})
    with pytest.raises(audiolab.AudioError):
        audiolab.normalise_session({"tracks": [
            {"source": "a.wav", "out_s": audiolab.MAX_SECONDS + 1}]})


def test_no_session_is_none_not_an_error(tmp_path):
    assert audiolab.load_session(tmp_path / "nothing.wav") is None


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------
@pytest.fixture()
def game(root):
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    audio = root / "assets" / "audio"
    audio.mkdir(parents=True)
    (audio / "sfx_hit.wav").write_bytes(_wav_bytes(seconds=0.3))
    audiolab.import_path(audio / "sfx_hit.wav").write_text(WAV_IMPORT, encoding="utf-8")
    (audio / "music_theme.ogg").write_bytes(_ogg_bytes(frames=44100 * 12))
    audiolab.import_path(audio / "music_theme.ogg").write_text(OGG_IMPORT, encoding="utf-8")
    return root


@pytest.fixture()
def client(game, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(game))
    with TestClient(app) as c:
        yield c


HIT = "assets/audio/sfx_hit.wav"
THEME = "assets/audio/music_theme.ogg"


def test_status_reports_what_can_be_written(client):
    d = client.get("/api/audio/lab/status").json()
    assert ".wav" in d["writable"]
    if not d["ogg"]:
        assert d["ogg_reason"], "an unavailable format must say why"


def test_the_listing_probes_and_flags_looping(client):
    d = client.get("/api/audio/lab/list").json()
    by = {s["rel"]: s for s in d["sounds"]}
    assert by[HIT]["seconds"] == pytest.approx(0.3, abs=0.01)
    assert by[THEME]["seconds"] == pytest.approx(12.0, abs=0.01)
    assert by[THEME]["loops"] is False


def test_open_returns_the_loop_state_and_a_playable_url(client):
    d = client.get("/api/audio/lab/open", params={"rel": THEME}).json()
    assert d["loop"]["importer"] == "ogg" and d["loop"]["enabled"] is False
    assert d["url"].startswith("/api/audio/file?rel=")
    assert d["info"]["sample_rate"] == 44100


def test_open_refuses_escapes_and_non_audio(client, game):
    assert client.get("/api/audio/lab/open",
                      params={"rel": "../../etc/passwd"}).status_code in (403, 415)
    (game / "notes.txt").write_text("x", encoding="utf-8")
    assert client.get("/api/audio/lab/open",
                      params={"rel": "notes.txt"}).status_code == 415


def test_save_writes_the_wav_and_keeps_the_old_bytes(client, game):
    before = (game / HIT).read_bytes()
    r = client.post("/api/audio/lab/save", json={
        "rel": HIT, "wav": base64.b64encode(_wav_bytes(seconds=0.6)).decode(),
        "overwrite": True})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["created"] is False
    assert (game / d["backup"]).read_bytes() == before
    assert audiolab.probe(game / HIT)["seconds"] == pytest.approx(0.6, abs=0.01)


def test_save_refuses_to_replace_an_existing_file_unasked(client, game):
    """A "save as" onto another sound's path sends no mtime, so the staleness
    check cannot fire. Without this guard it silently replaced that sound."""
    before = (game / HIT).read_bytes()
    r = client.post("/api/audio/lab/save", json={
        "rel": HIT, "wav": base64.b64encode(_wav_bytes(seconds=0.6)).decode()})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "exists"
    assert (game / HIT).read_bytes() == before


def test_save_can_create_a_sound_that_did_not_exist(client, game):
    """"Save as" is how a synthesised effect is born."""
    r = client.post("/api/audio/lab/save", json={
        "rel": "assets/audio/sfx_brand_new.wav",
        "wav": base64.b64encode(_wav_bytes(seconds=0.2)).decode()})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["created"] is True and d["backup"] is None
    assert d["needs_godot_import"] is True
    assert (game / "assets/audio/sfx_brand_new.wav").is_file()


def test_a_stale_mtime_is_refused(client, game):
    r = client.post("/api/audio/lab/save", json={
        "rel": HIT, "wav": base64.b64encode(_wav_bytes()).decode(), "mtime": 1})
    assert r.status_code == 409


def test_save_refuses_junk_and_unwritable_targets(client, game):
    assert client.post("/api/audio/lab/save",
                       json={"rel": HIT, "wav": ""}).status_code == 400
    assert client.post("/api/audio/lab/save",
                       json={"rel": HIT, "wav": "!!!"}).status_code == 400
    assert client.post("/api/audio/lab/save", json={
        "rel": HIT, "wav": base64.b64encode(b"not a wav").decode()}).status_code == 400
    (game / "assets/audio/voice.mp3").write_bytes(b"\xff\xfb")
    assert client.post("/api/audio/lab/save", json={
        "rel": "assets/audio/voice.mp3",
        "wav": base64.b64encode(_wav_bytes()).decode()}).status_code == 415


def test_the_loop_endpoint_sets_what_godot_reads(client, game):
    r = client.post("/api/audio/lab/loop", json={
        "rel": THEME, "enabled": True, "begin_s": 3.5})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["loop"]["enabled"] is True
    text = audiolab.import_path(game / THEME).read_text(encoding="utf-8")
    assert "loop=true" in text and "loop_offset=3.5" in text
    # And the listing now agrees, which is the whole point of surfacing it.
    listed = client.get("/api/audio/lab/list").json()["sounds"]
    assert next(s for s in listed if s["rel"] == THEME)["loops"] is True


def test_the_loop_endpoint_reports_a_bad_range_instead_of_writing(client, game):
    before = audiolab.import_path(game / HIT).read_text(encoding="utf-8")
    r = client.post("/api/audio/lab/loop", json={
        "rel": HIT, "enabled": True, "begin_s": 2.0, "end_s": 1.0})
    assert r.status_code == 400
    assert audiolab.import_path(game / HIT).read_text(encoding="utf-8") == before


def test_a_session_saves_beside_the_file(client, game):
    r = client.post("/api/audio/lab/session", json={
        "rel": HIT, "session": {"tracks": [
            {"source": THEME, "offset_s": 1.0, "gain_db": -12}]}})
    assert r.status_code == 200, r.text
    path = game / r.json()["data"]["path"]
    assert path.name == "sfx_hit.wav.mix.json"
    assert json.loads(path.read_text(encoding="utf-8"))["tracks"][0]["gain_db"] == -12


def test_a_session_with_an_escaping_source_is_refused(client):
    r = client.post("/api/audio/lab/session", json={
        "rel": HIT, "session": {"tracks": [{"source": "../../../etc/passwd"}]}})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Beat sessions — the pattern, not the render
# ---------------------------------------------------------------------------
def _beat(**over):
    base = {
        "bpm": 120, "swing": 0.0, "steps": 4, "resolution": 4,
        "patterns": [{"name": "A", "tracks": [
            {"name": "kick", "kind": "drum", "voice": "kick",
             "steps": [{"on": True}, {}, {"on": True, "vel": 0.5}, {}]}]}],
        "song": ["A"],
    }
    base.update(over)
    return base


def test_a_beat_round_trips_with_its_grid_intact(tmp_path):
    target = tmp_path / "loop.wav"
    saved = audiolab.save_beat(target, _beat())
    assert audiolab.beat_path(target).name == "loop.wav.beat.json"
    back = audiolab.load_beat(target)
    steps = back["patterns"][0]["tracks"][0]["steps"]
    assert [s["on"] for s in steps] == [True, False, True, False]
    assert steps[2]["vel"] == 0.5
    assert saved["updated_at"]


def test_missing_steps_are_filled_not_dropped():
    """A track that arrives short must become a full row of rests, or the grid
    the UI draws and the grid the file holds quietly disagree."""
    out = audiolab.normalise_beat(_beat(steps=8))
    assert len(out["patterns"][0]["tracks"][0]["steps"]) == 8


def test_an_unknown_voice_is_refused():
    with pytest.raises(audiolab.AudioError):
        audiolab.normalise_beat(_beat(patterns=[{"name": "A", "tracks": [
            {"kind": "drum", "voice": "banjo", "steps": []}]}]))


def test_a_sample_track_must_name_a_source_inside_the_project():
    with pytest.raises(audiolab.AudioError) as exc:
        audiolab.normalise_beat(_beat(patterns=[{"name": "A", "tracks": [
            {"kind": "sample", "source": "../../etc/passwd", "steps": []}]}]))
    assert "escapes" in str(exc.value)
    with pytest.raises(audiolab.AudioError):
        audiolab.normalise_beat(_beat(patterns=[{"name": "A", "tracks": [
            {"kind": "sample", "source": "", "steps": []}]}]))


def test_a_song_may_only_name_patterns_that_exist():
    """Otherwise the render silently drops a bar and the loop is short."""
    with pytest.raises(audiolab.AudioError) as exc:
        audiolab.normalise_beat(_beat(song=["A", "Z"]))
    assert "Z" in str(exc.value)


def test_out_of_range_tempo_and_grid_are_refused():
    for bad in ({"bpm": 4}, {"bpm": 900}, {"swing": 3}, {"steps": 0},
                {"steps": 999}, {"resolution": 5}):
        with pytest.raises(audiolab.AudioError):
            audiolab.normalise_beat(_beat(**bad))


def test_a_step_velocity_outside_zero_to_one_is_refused():
    with pytest.raises(audiolab.AudioError):
        audiolab.normalise_beat(_beat(patterns=[{"name": "A", "tracks": [
            {"kind": "drum", "voice": "kick", "steps": [{"on": True, "vel": 4}]}]}]))


def test_beat_seconds_matches_the_grid():
    """The UI prints this number next to the song; the render must agree."""
    session = audiolab.normalise_beat(
        _beat(bpm=120, steps=16, resolution=4, song=["A", "A", "A", "A"]))
    assert audiolab.beat_seconds(session) == pytest.approx(8.0)


def test_the_beat_endpoints_round_trip(client, game):
    r = client.post("/api/audio/lab/beat", json={"rel": HIT, "beat": _beat()})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["path"] == "assets/audio/sfx_hit.wav.beat.json"
    assert d["seconds"] == pytest.approx(0.5)
    got = client.get("/api/audio/lab/beat", params={"rel": HIT}).json()
    assert got["beat"]["patterns"][0]["tracks"][0]["name"] == "kick"
    assert "kick" in got["voices"]["drum"]
    # And opening the clip surfaces it, so the studio reopens on the pattern.
    assert client.get("/api/audio/lab/open",
                      params={"rel": HIT}).json()["beat"]["bpm"] == 120


def test_a_bad_beat_is_refused_by_the_endpoint(client):
    r = client.post("/api/audio/lab/beat",
                    json={"rel": HIT, "beat": _beat(song=["Q"])})
    assert r.status_code == 400


def test_no_beat_is_none_not_an_error(client):
    assert client.get("/api/audio/lab/beat",
                      params={"rel": THEME}).json()["beat"] is None


# ---------------------------------------------------------------------------
# The UI ships it
# ---------------------------------------------------------------------------
def test_the_audio_lab_is_loaded_and_reachable():
    # Source lives in frontend/public; bgate_ui/static is the Vite build output
    # (frontend/public copied verbatim, plus dist/).
    static = Path(__file__).resolve().parents[1] / "frontend" / "public"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/audiolab.js"' in html
    # THE WAY IN MOVED, THE RULE DID NOT. The "audio lab" button lives on the
    # React assets deck now (frontend/src/views/assets/Assets.tsx), so the call
    # is in the built bundle rather than in index.html. A lab with no entry
    # point is still the failure this asserts against.
    dist = (Path(__file__).resolve().parents[1]
            / "bgate_ui" / "static" / "dist" / "bgate.js")
    assert dist.is_file(), "no built bundle — run `cd frontend && npm run build`"
    assert "AudioLab" in dist.read_text(encoding="utf-8", errors="replace"), (
        "the lab needs a way in")
    js = (static / "audiolab.js").read_text(encoding="utf-8")
    for path in ("/api/audio/lab/open", "/api/audio/lab/save",
                 "/api/audio/lab/loop", "/api/audio/lab/session",
                 "/api/audio/lab/status", "/api/audio/file"):
        assert path in js
    # Audio families in the library open here rather than nowhere.
    lib = (static / "assetlib.js").read_text(encoding="utf-8")
    assert "AudioLab.open" in lib


def test_the_beat_maker_is_loaded_and_renders_through_the_clip_editor():
    """The studio must hand its render to the clip editor rather than owning a
    second save path — one place writes audio, and it takes the backup."""
    static = Path(__file__).resolve().parents[1] / "frontend" / "public"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/beatmaker.js"' in html
    bm = (static / "beatmaker.js").read_text(encoding="utf-8")
    assert "/api/audio/lab/beat" in bm
    assert "AudioLab.adopt" in bm, "the render must land in the clip editor"
    assert "OfflineAudioContext" in bm
    # Live and rendered output come from the same scheduling primitive.
    assert bm.count("function fire(") == 1
    # The comment explaining why it is absent is fine; a CALL is not.
    assert "Math.random(" not in bm, "a beat must render identically every time"
    lab = (static / "audiolab.js").read_text(encoding="utf-8")
    assert "BeatMaker.mount" in lab and "function adopt(" in lab
