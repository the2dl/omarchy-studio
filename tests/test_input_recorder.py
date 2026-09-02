"""The sampler's parsing and its bind-safety invariants.

Nothing here talks to Hyprland. The parts that do -- bind registration, the dead-man
timer, the sweep -- were exercised against the live compositor by hand; what is pinned
here is the text of the Lua that gets sent and the parsing of what comes back, because
those are what silently rot when someone edits the file.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "omarchy_studio_input_recorder",
    Path(__file__).resolve().parent.parent / "daemon" / "input_recorder.py",
)
ir = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ir
_SPEC.loader.exec_module(ir)


# --- spool parsing ----------------------------------------------------------


def collect(tmp_path, text):
    spool = tmp_path / "spool"
    spool.write_text(text)
    got = []
    tail = ir.SpoolTail(spool, lambda *a: got.append(a))
    tail.drain()
    return got, tail


def test_click_line_from_the_lua_handler(tmp_path):
    # hl.get_cursor_pos() returns floats; the Lua handler writes them verbatim.
    got, _ = collect(tmp_path, "click 272 767.07982268403293 407.13449105974769\n")
    assert got == [("click", "272", got[0][2], 767, 407)]


def test_every_button_and_scroll_direction(tmp_path):
    got, _ = collect(
        tmp_path,
        "click 272 1 2\nclick 273 3 4\nclick 274 5 6\n"
        "scroll up 7 8\nscroll down 9 10\n",
    )
    assert [(g[0], g[1]) for g in got] == [
        ("click", "272"), ("click", "273"), ("click", "274"),
        ("scroll", "up"), ("scroll", "down"),
    ]


def test_chapter_label_keeps_its_spaces(tmp_path):
    got, _ = collect(tmp_path, "chapter the good bit\n")
    assert got == [("chapter", "the good bit", got[0][2], 0, 0)]


def test_chapter_with_no_label(tmp_path):
    got, _ = collect(tmp_path, "chapter \n")
    assert got == [("chapter", "", got[0][2], 0, 0)]


def test_a_torn_trailing_line_is_held_until_it_is_complete(tmp_path):
    """Two writers append here and the tail polls every millisecond, so reading a
    half-written line is routine. It must be completed, not dropped or mangled."""
    spool = tmp_path / "spool"
    spool.write_text("click 272 10 20\nclick 273 30")
    got = []
    tail = ir.SpoolTail(spool, lambda *a: got.append(a))
    tail.drain()
    assert [(g[0], g[1]) for g in got] == [("click", "272")]

    with spool.open("a") as f:
        f.write(" 40\n")
    tail.drain()
    assert [(g[0], g[1], g[3], g[4]) for g in got] == [
        ("click", "272", 10, 20),
        ("click", "273", 30, 40),
    ]


def test_a_garbage_line_does_not_stop_the_next_one(tmp_path):
    got, _ = collect(tmp_path, "click 272 nope nope\nwhat\nclick 274 1 2\n")
    assert [(g[0], g[1]) for g in got] == [("click", "274")]


def test_drain_does_not_re_emit(tmp_path):
    got, tail = collect(tmp_path, "click 272 1 2\n")
    tail.drain()
    tail.drain()
    assert len(got) == 1


# --- reading hyprctl binds --------------------------------------------------

BINDS_SAMPLE = """bindd
\tmodmask: 64
\tsubmap:
\tkey: mouse:272
\tkeycode: 0
\tcatchall: false
\tdescription: Move window
\tdispatcher: __lua
\targ: 211

bindmd
\tmodmask: 0
\tsubmap:
\tkey: mouse:272
\tkeycode: 0
\tcatchall: false
\tdescription: OMARCHY-STUDIO-INPUT-272
\tdispatcher: __lua
\targ: 400
"""


def test_descriptions_are_read_off_the_real_output_shape():
    assert ir._DESC_RE.findall(BINDS_SAMPLE) == [
        "Move window",
        "OMARCHY-STUDIO-INPUT-272",
    ]


def test_the_users_own_mouse_binds_are_not_ours():
    """The user's SUPER+mouse:272 sits on the same key as ours at a different modmask.
    Only the description tells them apart, which is why the sweep matches on it."""
    ours = [d for d in ir._DESC_RE.findall(BINDS_SAMPLE) if d.startswith(ir.TAG)]
    assert ours == ["OMARCHY-STUDIO-INPUT-272"]


# --- the Lua that gets sent -------------------------------------------------


@pytest.fixture
def arm_lua():
    return ir.LUA_ARM % {
        "tag": ir.TAG,
        "spool": "/tmp/spool",
        "adds": "\n".join(f'add("mouse:{c}", "click", "{c}")' for c in ir.BUTTONS),
        "deadman_ms": 15000,
    }


def test_arm_interpolates_completely(arm_lua):
    assert "%(" not in arm_lua
    assert "/tmp/spool" in arm_lua
    for code in ("272", "273", "274"):
        assert f'add("mouse:{code}", "click", "{code}")' in arm_lua


def test_binds_are_non_consuming(arm_lua):
    """Without this the recorder eats every click the user makes."""
    assert "non_consuming = true" in arm_lua
    assert "mouse = true" in arm_lua


def test_the_handler_body_is_wrapped(arm_lua):
    """An uncaught error on the compositor's input path wedges the pointer for the
    whole session, so the handler body runs inside pcall."""
    body = arm_lua.split("local function handler", 1)[1].split("R.binds = {}", 1)[0]
    assert "pcall(function()" in body
    assert body.index("pcall(function()") < body.index("hl.get_cursor_pos()")


def test_arm_releases_before_it_binds(arm_lua):
    """Arming twice without this doubles every click."""
    assert arm_lua.index("R.release()") < arm_lua.index("R.binds = {}\nlocal function add")


def test_release_only_touches_descriptions_carrying_our_tag(arm_lua):
    release = arm_lua.split("R.release = function()", 1)[1].split("end\nR.release()", 1)[0]
    assert 'e.desc:sub(1, #R.tag) == R.tag' in release


def test_a_dead_man_timer_is_always_armed(arm_lua):
    assert 'type = "oneshot"' in arm_lua
    assert "timeout = 15000" in arm_lua
    assert "R.dead = hl.timer" in arm_lua


def test_the_heartbeat_re_arms_the_same_timer():
    beat = ir.LUA_HEARTBEAT % {"deadman_ms": 15000}
    assert "set_timeout(15000)" in beat
    assert ir.HEARTBEAT_S < ir.DEADMAN_S / 2, "a single missed beat must not expire"


def test_nothing_ever_unbinds_by_key_name(arm_lua):
    """The one mechanism that must never appear in this file.

    hl.unbind(key) takes a bare keysym with no modmask -- verified against a live
    compositor, where unbinding one key removed all four binds registered on it. Using
    it to clean up mouse:272 would also delete the user's SUPER+mouse:272 "Move window",
    which the user cannot get back without reloading their config. Removal goes through
    the HL.Keybind handle, always.
    """
    for source in (arm_lua, ir.LUA_RELEASE, ir.LUA_HEARTBEAT):
        assert "hl.unbind" not in source


def test_a_quote_in_the_bundle_path_cannot_break_the_lua():
    """`~/Videos/demo "final"/events` is a legal directory. Unescaped it closes the Lua
    string early and the binds never register, which surfaces as a missing click track
    long after the recording is over."""
    lua = ir.LUA_ARM % {
        "tag": ir.lua_str(ir.TAG),
        "spool": ir.lua_str('/home/dan/demo "final"\\x/spool'),
        "adds": "",
        "deadman_ms": 15000,
    }
    literal = lua.split("R.spool = ", 1)[1].split("\n", 1)[0]
    assert literal == '"/home/dan/demo \\"final\\"\\\\x/spool"'
    # The Lua escape sequences must decode back to the path the daemon meant.
    assert literal[1:-1].replace('\\"', '"').replace("\\\\", "\\") == (
        '/home/dan/demo "final"\\x/spool'
    )


def test_the_wrapper_sweep_matches_the_daemons():
    """bin/omarchy-capture-input-events has its own copy of the release Lua for the
    case where the daemon is already dead. The two must stay in step."""
    wrapper = (Path(__file__).resolve().parent.parent
               / "bin" / "omarchy-capture-input-events").read_text()
    assert "if R and R.release then pcall(R.release) end" in wrapper
    assert ir.TAG in wrapper
