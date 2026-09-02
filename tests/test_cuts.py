from __future__ import annotations

import pytest
from ffmpeg_harness import framehashes, make_counter_clip, needs_ffmpeg

from omarchy_studio.cuts import CutError, cut_chain, cut_labels, cut_output_label
from omarchy_studio.timebase import CutMap, FrameRange, Timebase

TB = Timebase(30)


def cm(cuts, total=60):
    return CutMap([FrameRange(*c) for c in cuts], total)


# -- structure ---------------------------------------------------------------


def test_no_cuts_emits_nothing_and_labels_pass_through():
    c = cm([])
    assert cut_chain(c, TB, ["[base]"], True) == ""
    assert cut_labels(c, ["[base]"], True) == {"[base]": "[base]", "[0:a]": "[0:a]"}


def test_labels_are_deterministic_when_cutting():
    c = cm([(10, 20)])
    assert cut_labels(c, ["[base]", "[cam]"]) == {
        "[base]": "[base_cut]",
        "[cam]": "[cam_cut]",
    }
    assert cut_output_label("[0:a]") == "[0_a_cut]"


def test_one_cut_in_the_middle_keeps_two_segments():
    g = cut_chain(cm([(10, 20)]), TB, ["[base]"])
    assert "[base]split=2[base_s0][base_s1]" in g
    assert "trim=start_frame=0:end_frame=10" in g
    assert "trim=start_frame=20:end_frame=60" in g
    assert "concat=n=2:v=1:a=0[base_cut]" in g


def test_never_uses_select():
    """aselect quantizes audio removal to whole ~21.3 ms decoder frames regardless of
    the video grid, which is what put ~50 ms of A/V skew into six cuts."""
    g = cut_chain(cm([(10, 20), (30, 35)]), TB, ["[base]"], True)
    assert "select" not in g and "aselect" not in g
    assert "trim=" in g and "atrim=" in g


def test_cut_touching_frame_zero():
    g = cut_chain(cm([(0, 15)]), TB, ["[base]"])
    assert "split=1[base_s0]" in g
    assert "trim=start_frame=15:end_frame=60" in g
    assert "concat=n=1:v=1:a=0[base_cut]" in g


def test_cut_touching_the_end():
    g = cut_chain(cm([(50, 60)]), TB, ["[base]"])
    assert "trim=start_frame=0:end_frame=50" in g
    assert "concat=n=1" in g


def test_many_cuts():
    cuts = [(i * 6, i * 6 + 2) for i in range(1, 9)]
    c = cm(cuts)
    g = cut_chain(c, TB, ["[base]"])
    assert f"split={len(c.kept)}" in g
    assert g.count("trim=start_frame=") == len(c.kept)


def test_audio_rides_with_the_first_video_label():
    g = cut_chain(cm([(10, 20)]), TB, ["[base]", "[cam]"], True)
    # One concat carries both streams -- that pairing is the reason for using concat.
    assert "concat=n=2:v=1:a=1[base_cut][0_a_cut]" in g
    assert "concat=n=2:v=1:a=0[cam_cut]" in g


def test_audio_boundaries_sit_on_frame_times():
    g = cut_chain(cm([(15, 30)]), TB, ["[base]"], True)
    assert "atrim=0.000000:0.500000" in g  # frame 15 at 30 fps
    assert "atrim=1.000000:2.000000" in g


def test_audio_only():
    g = cut_chain(cm([(10, 20)]), TB, [], True)
    assert "concat=n=2:v=0:a=1[0_a_cut]" in g


def test_camera_is_cut_too_or_it_drifts():
    """An uncut webcam drifts by exactly the total cut duration -- measured at exactly
    -90 and -150 frames for 3 s and 5 s of cuts."""
    g = cut_chain(cm([(10, 20)]), TB, ["[base]", "[cam]"])
    assert g.count("trim=start_frame=0:end_frame=10") == 2


def test_rejects_duplicate_labels():
    with pytest.raises(CutError):
        cut_chain(cm([(10, 20)]), TB, ["[base]", "[base]"])


def test_rejects_audio_label_listed_as_video():
    with pytest.raises(CutError):
        cut_chain(cm([(10, 20)]), TB, ["[0:a]"], True, audio_label="[0:a]")


def test_rejects_cutting_everything():
    with pytest.raises(CutError):
        cut_chain(cm([(0, 60)]), TB, ["[base]"])


def test_rejects_no_streams():
    with pytest.raises(CutError):
        cut_chain(cm([(10, 20)]), TB, [])


# -- it has to survive contact with ffmpeg -----------------------------------


@needs_ffmpeg
@pytest.mark.parametrize(
    "cuts",
    [
        [(10, 20)],
        [(0, 12)],
        [(48, 60)],
        [(5, 9), (20, 26), (40, 41)],
        [(0, 3), (57, 60)],
    ],
    ids=["middle", "head", "tail", "many", "both-edges"],
)
def test_exactly_the_kept_frames_survive(cuts, tmp_path):
    """Frame counts are not enough: a boundary that leaks one frame keeps the count
    right while shifting content, which is how 318 of 899 boundaries hid. Comparing the
    hash SEQUENCE against the source's makes the shift visible."""
    clip = make_counter_clip(tmp_path / "src.mkv")
    src = framehashes("[0:v]null[vout]", tmp_path, inputs=[["-i", str(clip)]])
    assert len(src) == 60

    c = cm(cuts)
    graph = cut_chain(c, TB, ["[0:v]"]) + ";[0_v_cut]null[vout]"
    got = framehashes(graph, tmp_path, inputs=[["-i", str(clip)]], maps=["[vout]"])

    expected = [src[i] for k in c.kept for i in range(k.start, k.end)]
    assert got == expected


@needs_ffmpeg
def test_video_and_audio_come_out_the_same_length(tmp_path):
    """concat=v=1:a=1 is chosen precisely so the two stay together; verify it does."""
    import subprocess

    from ffmpeg_harness import FFMPEG

    clip = make_counter_clip(tmp_path / "src.mkv")
    c = cm([(10, 20), (40, 46)])
    # A filter output can only be consumed once, so the audio is asplit for the
    # second output below.
    graph = (
        cut_chain(c, TB, ["[0:v]"], True)
        + ";[0_v_cut]null[vout];[0_a_cut]asplit=2[aout1][aout2]"
    )
    gp = tmp_path / "g.txt"
    gp.write_text(graph + "\n")
    out = tmp_path / "out.mkv"
    wav = tmp_path / "out.wav"
    r = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
            "-/filter_complex", str(gp),
            "-map", "[vout]", "-map", "[aout1]",
            "-c:v", "ffv1", "-c:a", "pcm_s16le", str(out),
            # Matroska records neither a frame count nor an audio duration for these
            # codecs, so the audio is written again as a wav purely to measure it.
            "-map", "[aout2]", str(wav),
        ],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr[-3000:]

    def probe(args: list[str], path) -> str:
        return subprocess.run(
            ["ffprobe", "-v", "error", "-of", "csv=p=0", *args, str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    frames = int(
        probe(
            ["-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames"], out
        )
    )
    assert frames == c.output_frames == 44
    audio_seconds = float(probe(["-show_entries", "format=duration"], wav))
    # Within one video frame of the video duration.
    assert abs(audio_seconds - c.output_frames / TB.fps) < 1.0 / TB.fps
