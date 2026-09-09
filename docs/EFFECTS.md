# Effects

An inspiration reference, not a spec. What people actually do with a
[`blink(1)`](https://blink1.thingm.com/), why those effects read well on two
LEDs at desk distance, and how each one maps onto this repo's `scenes` config.

Every JSON block here is a fragment you can paste straight into the `scenes`
object in `blink-light.json`, then check with:

```bat
.venv\Scripts\python.exe -m blink_light config validate
```

`tests/test_effects_doc.py` validates every block on this page against the real
schema, so nothing here can drift into being unpasteable.

---

## The mechanics that decide how an effect looks

Five facts about playback do most of the work. They are worth knowing before
inventing a scene, because each one is a place a design silently fails.

**1. A step is a fade, never a jump.** `seconds` is the *time taken to arrive
at* `color`, not a hold at it — `blink_light/device.py` writes each step as one
`write_pattern_line(millis, color, index, led)`, which is
[blink1-python's fade primitive](https://github.com/todbot/blink1-python). A
step of `0.45` seconds is a 450 ms glide. This is why the shipped notification
scenes read as *breaths* rather than blinks.

**2. To hold a colour, repeat it.** There is no hold primitive. A second step
with the same colour "fades" to where the light already is, which is a hold of
that length. Two steps of `#FFB300` back to back give you a fade-in then a
plateau.

**3. A short `seconds` is how you get a hard edge.** Anything at or under about
`0.08` reads as an instant switch. Long fades read as ambient, short fades read
as an alarm — that difference matters more than colour does at peripheral
vision.

**4. `led` is a relay, not a crossfade.** `0` is both LEDs, `1` is the top one,
`2` is the bottom — the same convention as
[Blink1Control's `{color, time, ledn}` tuples](https://github.com/todbot/Blink1Control2/blob/main-es5/NOTES.md)
and `blink1-tool -l`. But steps still play strictly in sequence, so two steps
addressing different LEDs happen one after the other. You cannot fade both LEDs
in opposite directions *simultaneously*; you can only hand the light back and
forth quickly enough that it looks like motion. `rainbow_swirl` in
`blink_light/defaults.py` is exactly this trick — alternating LEDs, held half a
hue-turn apart.

**5. Under 32 steps and non-looping, the device plays it; otherwise the host
does.** `can_run_scene_on_device()` uploads scenes of 32 steps or fewer to the
mk2/mk3's 32-line pattern memory
([tod's mk2 tricks](https://github.com/todbot/blink1/blob/main/docs/blink1-mk2-tricks.md)),
so timing stays smooth while the machine is busy. Go over 32 steps, or set
`loop: true`, and playback falls back to a host thread issuing one fade per
step — still correct, but subject to whatever else the CPU is doing. **32 steps
is the line worth designing under.** (Only the first 16 lines survive a
`--savepattern` to flash, but this repo never writes flash — it uploads to RAM
on every play.)

Two smaller ones:

- The first step fades **from whatever the light is currently showing**, because
  stopping a pattern halts playback without blanking the LEDs. A notification
  played over the watcher's resting green enters from green.
- `repeat` is a finite count (`>= 1`, enforced by `config validate`); `loop:
  true` is forever. A `show` scene must be non-looping and must fit inside
  `show.max_seconds` — 10s today, checked at validation time rather than by
  truncating playback.

---

## Effects worth stealing

### Breathing — the one that is always right for "state"

The single most common blink(1) idiom, and the one ThingM's own framing of
["glanceable notice"](https://blink1.thingm.com/) is built around: a slow fade
up and down that you notice only when you choose to look.

The trick that separates a good breath from a blinky one is **not fading to
black**. Bottom out at a dim version of the same hue instead. The light then
reads as alive and steady rather than as something repeatedly turning off.
`rainbow_swirl` uses the same rule — its brightness envelope floors at 45%.

```json
"breathe_amber_scene": {
  "loop": true,
  "steps": [
    { "color": "#FFB300", "seconds": 1.6 },
    { "color": "#3A2600", "seconds": 1.6 }
  ]
}
```

Two steps, looping, ~3.2s a cycle. Good as a `rules` action or an `override`
preset for "heads down" — long enough that it never pulls your eye.

### Heartbeat — coding meaning into rhythm, not colour

Two quick thumps and a long rest. Peripheral vision resolves rhythm far better
than it resolves hue, so a heartbeat is distinguishable from a breath across a
room even if both are the same colour. That is the whole reason this repo's
`agent_done` (two slow breaths) and `agent_blocked` (three quick ones) differ in
cadence as well as colour.

```json
"heartbeat_scene": {
  "loop": true,
  "steps": [
    { "color": "#C62828", "seconds": 0.12 },
    { "color": "#000000", "seconds": 0.10 },
    { "color": "#C62828", "seconds": 0.12 },
    { "color": "#000000", "seconds": 1.30 }
  ]
}
```

The long dark tail is doing the work. Shorten it and this becomes a strobe.

### The relay — motion across two LEDs

The classic mk2 demo is
[`blink1-tool -l 1 --red && blink1-tool -l 2 --blue`](https://github.com/todbot/blink1/blob/main/docs/blink1-mk2-tricks.md),
then reversed. Handing colour back and forth between the two LEDs is the only
real "movement" a blink(1) has, and at desk distance it is surprisingly
legible — the eye reads direction even from a 2-pixel display.

```json
"relay_scene": {
  "loop": true,
  "steps": [
    { "color": "#00B0FF", "seconds": 0.35, "led": 1 },
    { "color": "#001426", "seconds": 0.35, "led": 2 },
    { "color": "#001426", "seconds": 0.35, "led": 1 },
    { "color": "#00B0FF", "seconds": 0.35, "led": 2 }
  ]
}
```

Because steps are sequential (mechanic 4), each LED changes on its own beat and
the pair never lands on the same colour at the same time. Keep the "off" end a
dim tint rather than `#000000` so the motion stays a slide rather than a
flicker.

### Candle flicker — irregularity is the effect

Warm hues around orange, with **deliberately uneven step durations**. Regular
timing reads as machinery; uneven timing reads as a flame. Nothing else about
this scene is clever — it is just seven arbitrary amber-to-orange stops with
seconds that never repeat.

```json
"candle_scene": {
  "loop": true,
  "steps": [
    { "color": "#FF8F00", "seconds": 0.18 },
    { "color": "#FFB74D", "seconds": 0.09 },
    { "color": "#E65100", "seconds": 0.24 },
    { "color": "#FFA726", "seconds": 0.07 },
    { "color": "#F57C00", "seconds": 0.31 },
    { "color": "#FFCC80", "seconds": 0.12 },
    { "color": "#EF6C00", "seconds": 0.21 }
  ]
}
```

Same family as tod's
[programmable nightlight](https://github.com/todbot/blink1/wiki/Using-blink(1)-as-a-programmable-nightlight),
which leans on long fades and long dark rests to stay sleep-friendly. If you
want the nightlight version, multiply every `seconds` by four.

### Two-colour strobe — for the things that must interrupt

Alternating red and blue on opposite LEDs, hard-edged. This is the effect to
reach for when you genuinely want to break concentration, and to avoid
otherwise: it is unpleasant on purpose. Non-looping with a `repeat` so it ends
itself and hands the light back to the watcher.

```json
"alert_strobe_scene": {
  "loop": false,
  "repeat": 4,
  "steps": [
    { "color": "#D50000", "seconds": 0.07, "led": 1 },
    { "color": "#000000", "seconds": 0.07, "led": 1 },
    { "color": "#2962FF", "seconds": 0.07, "led": 2 },
    { "color": "#000000", "seconds": 0.07, "led": 2 }
  ]
}
```

1.12s total, 4 steps × 4 repeats. Wire it to a `notify` event, never to
anything hourly.

### Sunrise ramp — an alternative daily show

A drop-in replacement for `rainbow_swirl` if you would rather the 17:00 show
felt like dusk than like a party. Deep red climbing through orange to a washed
warm yellow and back down, both LEDs together, ending on a clean fade to black
so the watcher's repaint is not fighting a leftover glow.

19 steps, 9.5s — inside both the 32-step device limit and `show.max_seconds`.

```json
"sunrise_show": {
  "loop": false,
  "steps": [
    { "color": "#0F0002", "seconds": 0.5 },
    { "color": "#590207", "seconds": 0.5 },
    { "color": "#850506", "seconds": 0.5 },
    { "color": "#A8120A", "seconds": 0.5 },
    { "color": "#C52310", "seconds": 0.5 },
    { "color": "#DC3717", "seconds": 0.5 },
    { "color": "#ED4B1D", "seconds": 0.5 },
    { "color": "#F95E24", "seconds": 0.5 },
    { "color": "#FE702A", "seconds": 0.5 },
    { "color": "#FE802F", "seconds": 0.5 },
    { "color": "#F98B33", "seconds": 0.5 },
    { "color": "#ED9236", "seconds": 0.5 },
    { "color": "#DC9336", "seconds": 0.5 },
    { "color": "#C58D35", "seconds": 0.5 },
    { "color": "#A88131", "seconds": 0.5 },
    { "color": "#856C29", "seconds": 0.5 },
    { "color": "#594C1D", "seconds": 0.5 },
    { "color": "#0F0E05", "seconds": 0.5 },
    { "color": "#000000", "seconds": 0.5 }
  ]
}
```

Point `show.scene` at it and re-validate. Note that a hand-written ramp like
this is the awkward case for the 32-step limit: at 0.5s a step you get 19 steps
in 9.5s, but halve the step time for a smoother ramp and you need 38 steps,
which pushes playback onto the host. That is the trade `rainbow_swirl` resolves
by generating its steps in `defaults.py` rather than hand-listing them.

---

## Playing with them

```bat
blink-light.bat light scene heartbeat_scene
blink-light.bat config validate
```

`light scene` plays one immediately, which is the fastest way to judge a design.
A looping scene played this way runs until something else repaints the light —
`light off` or the watcher's next tick.

Then wire it to whatever should trigger it:

| Want it… | Put the scene name in |
| --- | --- |
| at a wall-clock time | an `alarms` entry's `action.scene` |
| when an external tool says so | a `notify` event |
| at 17:00 daily | `show.scene` (non-looping, under `max_seconds`) |
| as a background state | a `rules` entry, a `preset`, or a routine phase |

Looping scenes belong in the *state* row only. Anything one-shot — alarm,
notify, show — must terminate on its own, or the light never returns to what the
watcher was showing.

---

## What other people wire them to

Ideas, mostly from the [`blink1` topic on GitHub](https://github.com/topics/blink1),
that this repo's `notify` events could serve without any code change:

- **Build and CI status** — the original blink(1) use case, and still the most
  common: [BuildBlink](https://github.com/brettswift/BuildBlink), a
  [Visual Studio build indicator](https://github.com/DigitalInBlue/build-status-indicator),
  and [`cargo-blinc`](https://github.com/devzbysiu/cargo-blinc) for running a
  command and colouring the result.
- **Branch / PR status** — [a blink(1) GitHub status light](https://coreyja.com/posts/blink-1-github-status/):
  green for success, red for failure, flashing yellow for pending. The flashing
  pending state is the interesting bit — motion for "in progress", steady for
  "settled" is a good general rule.
- **Pomodoro** — [blink1-pomodoro](https://github.com/mollietaylor/blink1-pomodoro).
  This repo already has `timer` and `routines`; a phase action is all it takes.
- **Server heartbeat** — blink1-tool's
  [`--servertickle`](https://github.com/todbot/blink1/blob/main/docs/blink1-tool-tips.md)
  makes the *device* play a stored pattern when the host stops checking in, so
  the light reports a hung machine that could not possibly send a command. Not
  wired up here, but it is the one effect a host-driven design cannot fake.

---

## Limits, in one table

| Limit | Value | Enforced by |
| --- | --- | --- |
| Steps playable on-device | 32 | `BlinkDeviceController.can_run_scene_on_device()` — over it, host playback |
| Pattern lines saved to flash | 16 | Device firmware; not used by this repo |
| Daily show length | `show.max_seconds`, 10s | `config validate`, which reports the measured duration |
| `repeat` | integer ≥ 1 | `config validate` |
| `led` | `0`, `1`, or `2` | `config validate` |
| `seconds` | ≥ 0 | `config validate` |

---

## Sources

- [blink(1) — ThingM](https://blink1.thingm.com/)
- [blink1-tool docs](https://github.com/todbot/blink1/blob/main/docs/blink1-tool.md) · [tips and pattern examples](https://github.com/todbot/blink1/blob/main/docs/blink1-tool-tips.md) · [mk2 tricks](https://github.com/todbot/blink1/blob/main/docs/blink1-mk2-tricks.md)
- [Making a customized flashing pattern](https://gist.github.com/todbot/be18f570e4140d6a29e429dbd6826f5f) · [blink(1) as a programmable nightlight](https://github.com/todbot/blink1/wiki/Using-blink(1)-as-a-programmable-nightlight)
- [Blink1Control2 pattern data model](https://github.com/todbot/Blink1Control2/blob/main-es5/NOTES.md)
- [blink1-python](https://github.com/todbot/blink1-python) — the library this repo drives the device through
- [`blink1` on GitHub](https://github.com/topics/blink1)
