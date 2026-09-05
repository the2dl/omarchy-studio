# beguiling-drafter.rnnn

An RNNoise model, used by `render.py` for the "remove keyboard clicks" option
(ffmpeg's `arnndn` filter). Taken verbatim from
<https://github.com/GregorR/rnnoise-models> (`beguiling-drafter-2018-08-30`).

That repository states of its models: "With the exception of the tools/ directory
and this file, none of this work is creative and thus none of it is subject to
copyright." It is vendored rather than downloaded so an export works offline and
does not depend on a host staying up.

Its table maps expected signal against expected noise; `beguiling-drafter` is the
"Recording noise, Voice signal" cell, which is what a screencast narration track
is -- a microphone in a room, and a person who is not only speaking.

Chosen by measurement, not by that table: see `_declick_chain` in `render.py`.
