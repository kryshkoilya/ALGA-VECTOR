# ALGA VECTOR — Civil Early Warning v0.6 design system

## Product and users

Offline-first Windows desktop application for civilian early warning and
incident awareness at enterprises, warehouses and industrial sites. It
correlates passive acoustic observations, measured RF activity, validated
external direction findings and cooperative civilian ADS-B context. It must
serve two skill levels:

- Beginner: guided setup, plain-language explanations, visible data provenance, safe defaults and a single recommended next action.
- Expert: dense metrics, raw parameters, calibration details, quality flags, replay and diagnostics without hiding uncertainty.

No screen may infer nationality, military status, hostility, ownership or an
exact platform identity. RF-only observations remain evidence-supported signal
families. Acoustic observations remain safe phenomenological classes such as
`rotor-like`, `engine-like`, `broadband anomaly` and `unknown aerial-like`.
ADS-B is cooperative civilian context, never IFF or a complete air picture.
Every fused incident carries measured evidence, a heuristic evidence-strength
score, alternatives, missing confirmations and limitations. The score is never
presented as calibrated probability.

## Visual constraints

- Continue the approved **ALGA VECTOR — Operational Interface (Navigation Refinement)** direction and retain its compact, fixed desktop shell.
- Premium dark cinematic treatment with restrained translucent tonal layering:
  graphite surfaces, subtle glass-like depth and one-pixel borders. Avoid blur
  behind live plots, excessive transparency, decorative 3D, neon glow and visual
  noise. Readability and stable rendering take priority over spectacle.
- Golos Text, 12 px minimum body text.
- Fixed 112 px left navigation, 64 px top header, 28 px footer.
- Palette: background `#050707`, navigation `#0A0F0E`, surface `#111817`, alternate surface `#16201E`, borders `#1D2926/#344540`, text `#EDF4F1`, secondary `#A6B2AD`, muted `#707D78`.
- Semantic accents only: ready `#25C78D`, info `#35B7AA`, warning `#E1A84B`, critical `#E35B65`.
- Radius 5–10 px, thin borders, very soft short shadows only where they clarify
  hierarchy.
- Minimum click target 32 px; navigation rows 49 px.

## Information architecture

Routes: Dashboard, Devices, Spectrum/RF, Acoustic, Direction, Situation,
Event Journal, Diagnostics, Settings and Demo/Replay.

The first production increment must make Dashboard, Devices, Spectrum/RF,
Direction, Event Journal, Diagnostics and Settings executable. Acoustic,
civilian ADS-B and Fusion must have real core contracts plus deterministic fake
sources. Situation and Replay may enter as explicit capability-gated modules;
an unavailable capability is shown honestly rather than represented by an
inactive button.

`Direction` is a bearing-observation workspace, not a radar and not a target
locator. It shows a 360° dial, sectors, bearing marker, uncertainty cone,
history trail and source only when a validated source exists. Supported source
modes are:

- `Manual`: operator-entered reference; explicitly labelled unmeasured.
- `External sensor`: accepted only with fresh data, current calibration and
  quality metadata.
- `Simulated`: available only in Training and permanently marked simulation.

Without a valid source the primary state is `Direction unavailable`, followed
by the exact reason and one safe next action. Never infer bearing or distance
from a single receiver's signal level.

`Situation` is an offline operational context view. It may show the explicitly
configured enterprise site, permitted zones, cooperative civilian ADS-B marks,
replay-time incident evidence and a bearing ray from a validated DF source. It
must never fabricate a target position from a bearing, draw range rings without
an actual ranging sensor, or turn an RF/acoustic event into geographic
coordinates.

## Required UX patterns

- Persistent mode switch: `SIMPLE MODE` / `EXPERT MODE`; the existing
  `guided` / `expert` configuration values remain the backward-compatible
  persistence contract. Switching changes navigation and presentation only,
  never acquisition, thresholds or measurement math.
- `SIMPLE MODE` opens on `Простая обстановка` and consumes only
  `snapshot.operator_situation`. It must not reconstruct conclusions from IQ,
  waterfall, RSSI, raw spectrum or legacy detector fields.
- The simple page has one dominant situation card, then exactly three
  operator questions: `Где`, `Насколько подтверждено`, `Что делать дальше`.
  A short recent-event list and `Показывать только важное` filter follow.
- Simple situation modes are `Тишина`, `Фон`, `Активность` and
  `Подтверждённая цель`. The last state is reserved for policy-approved
  `TARGET_CONFIRMED`; generic RF+acoustic correlation remains an anomaly and
  cannot silently enter the confirmed-target state.
- `EXPERT MODE` retains Spectrum, Events, Direction, Map and Diagnostics.
  The map is expert-only and never fabricates a target location from one
  bearing or signal level.
- Every recommendation includes `Why`, `Evidence`, `Evidence strength`,
  `Missing confirmation`, `Limitations` and `What to do next`.
- Dashboard has one dominant incident-state card, a four-sensor readiness row
  (`Acoustic`, `RF`, `DF`, `Civil ADS-B`) and one recent-event timeline. The
  novice view shows no more than three evidence points and one next action.
- A multi-sensor warning is visually stronger only after temporal confirmation
  and independent acoustic + RF evidence. A single-sensor anomaly remains
  clearly marked `unconfirmed`.
- A compact non-modal RF observation bar may appear under the global header. It
  names only an evidence-supported signal family and links to Signal Events.
  Its tooltip carries measured evidence, confidence wording, alternatives and
  the explicit object-identification limitation.
- Primary event data: center frequency, occupied bandwidth, measured level,
  SNR-like/peak-excess metric (clearly labelled when uncalibrated), duration,
  signal family, confidence, data source and quality flags.
- Device tuning controls derive their range and bandwidth from the selected
  hardware capability. Unsupported values are blocked before acquisition and
  explained in plain Russian.
- HackRF/PortaPack support is receive-only. PortaPack is visible to the desktop
  application only while the hardware exposes standard HackRF USB mode.
- tinySA Ultra high-frequency/Ultra-mode views carry visible sweep, mirror,
  short-burst and wideband limitations.
- A guided first-run checklist links directly to each missing prerequisite.
- Expert diagnostics expose raw device/source provenance and quality flags.
- All empty/error/degraded states are actionable and non-modal.
- Missing microphone, SDR, DF source, ADS-B feed or map tiles degrades only the
  affected capability. It never crashes the shell or silently changes demo
  data into live data.

## Motion

No decorative animation. Use only status transitions, progress, a low-rate
activity indicator and a short bearing-trail fade. Respect reduced motion.

## Required direction screen composition

- Header: `Направление`, source badge, freshness and calibration state.
- Main left area: large 360° dial with cardinal labels, 30° ticks, active bearing,
  uncertainty cone and recent trail.
- Main right rail: source mode, bearing, uncertainty, heuristic confidence,
  timestamp, evidence quality and limitations.
- Bottom event strip: recent RF episodes with frequency, family and duration;
  selecting an episode does not fabricate a bearing.
- Empty state occupies the dial itself and explains why direction is unavailable.
- Never show distance rings in kilometres, a geographic position or target icon.

## Required dashboard composition

- Header: `Гражданское раннее предупреждение`, profile, mode and overall health.
- Primary state: `Наблюдение спокойно`, `Неподтверждённая аномалия`,
  `Подтверждённый многосенсорный инцидент` or `Данные ненадёжны`.
- Primary card explains measured facts first, then correlation and limitations.
- Sensor row: one compact card per Acoustic/RF/DF/Civil ADS-B with
  ready/degraded/unavailable state, freshness and provenance.
- Recent timeline: timestamp, generic event family, contributing sensors,
  evidence strength and lifecycle.
- Guided mode has one primary button. Expert mode may expose evidence tables,
  quality flags and source IDs.
- No wording may say that an aircraft type, nationality or hostile intent was
  established.

## Product signature

The footer always includes `Разработал: Буйвол и Задира`.
