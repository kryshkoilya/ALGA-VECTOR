# ALGA VECTOR 1.0 — Target-centric operator platform design system

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

The primary product model is `event → fused target → operator presentation`.
In this context a target is a bounded operational grouping of compatible,
time-correlated observations. It is not automatically a physical-object
identity. Several normalized events may contribute to one target; stale targets
decay and retire instead of remaining as permanent alerts.

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
- Important status text: 30–36 px; target title: 21–24 px; card title: 13–14 px;
  body: 12–14 px. Numeric bearing, frequency and timestamps use tabular figures.
- Tonal hierarchy: `#050707` canvas → `#0A0F0E` shell → `#111817` primary
  surface → `#16201E` raised/interactive surface. Native Qt cannot provide
  reliable background blur, so “glass” means restrained tonal layering and
  one-pixel translucent-looking borders, never fake blur or low-contrast text.

## Information architecture

Simple routes: Situation, Devices, Events, Direction and Settings.

Expert routes: Situation, Overview, Targets, Devices, Spectrum/RF, Events,
Direction, Map, Diagnostics and Settings.

The 1.0 RC ships Situation as the default executable decision surface.
Dashboard, Devices, Spectrum/RF, Direction, Event Journal, Diagnostics and
Settings remain executable. Acoustic, civilian ADS-B and Fusion have real core
contracts plus deterministic fake sources. Replay remains capability-gated; an
unavailable capability is shown honestly rather than represented by an inactive
button.

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

## Target presentation contract

Every target presentation has:

- `target_id` and lifecycle;
- `technical_label`, `operator_label`, `operator_explanation`;
- verbal confirmation stage;
- source attribution and the sensors actually used;
- validated direction/sector/zone or an explicit unavailable state;
- first/last seen timestamps;
- short and detailed recommendations;
- evidence strength for Expert Mode only;
- alternatives, missing confirmation and limitations.

Confirmation stages shown in SIMPLE MODE:

1. `Фон`
2. `Подозрительная активность`
3. `Вероятный источник`
4. `Вероятная цель`
5. `Подтверждённая цель`

The final stage is allowed only for a policy-approved `TARGET_CONFIRMED`
normalized event with the required independent physical confirmations. SIMPLE
MODE never displays a numeric percentage. Expert target breakdown may show the
raw heuristic score with a persistent “not a calibrated probability” label.

## Required UX patterns

- Persistent mode switch: `SIMPLE MODE` / `EXPERT MODE`; the existing
  `guided` / `expert` configuration values remain the backward-compatible
  persistence contract. Switching changes navigation and presentation only,
  never acquisition, thresholds or measurement math.
- `SIMPLE MODE` opens on `Простая обстановка` and consumes only interpreted
  snapshot contracts: `operator_situation`, `current_target`/`targets` and
  `sensor_readiness`. It must not reconstruct conclusions from IQ, waterfall,
  RSSI, raw spectrum or legacy detector fields.
- SIMPLE MODE is the default decision surface. From top to bottom it contains:
  hero status, current target, compact sector/zone, recommended action, 3–5
  important events and a seven-sensor readiness strip.
- The current-target region answers exactly four questions in reading order:
  `Что это`, `Где`, `Насколько подтверждено`, `Что делать дальше`.
- A short recent-event list and `Показывать только важное` filter follow.
- Simple situation modes are `Тишина`, `Фон`, `Активность` and
  `Подтверждённая цель`. The last state is reserved for policy-approved
  `TARGET_CONFIRMED`; generic RF+acoustic correlation remains an anomaly and
  cannot silently enter the confirmed-target state.
- `EXPERT MODE` retains Spectrum, Events, Direction, Map and Diagnostics.
  The map is expert-only and never fabricates a target location from one
  bearing or signal level.
- `EXPERT MODE` adds a target breakdown workspace grouped by task: target
  lifecycle, source attribution, evidence, raw confidence, direction validity,
  limitations and replay/calibration context.
- Every recommendation includes `Why`, `Evidence`, `Evidence strength`,
  `Missing confirmation`, `Limitations` and `What to do next`.
- The readiness strip always reserves stable positions for `TinySA`, `RTL-SDR`,
  `KrakenSDR`, `Acoustic`, `ADS-B`, `Passive radar` and `Fusion`. Each tile has
  `ready`, `limited` or `unavailable`, one short reason and its operational
  impact. Missing hardware does not collapse or reorder the strip.
- The novice view shows no more than one current target, one verbal confirmation
  stage, five important events and one primary next action.
- Visual escalation follows the normalized event or target confirmation stage
  and policy-approved evidence; it is never inferred from a hard-coded
  acoustic + RF pair. Generic multi-sensor correlation and a single-sensor
  anomaly remain clearly marked `unconfirmed`.
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

No decorative animation. Use 120–180 ms easing only for status-color
transitions, card replacement, progress, a low-rate activity indicator and a
short bearing-trail fade. Never animate live plots through the main UI thread.
Respect reduced motion.

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

## Required SIMPLE MODE composition

- Hero: verbal environment state and one plain-language explanation.
- Current target: probable general type, target ID only as quiet metadata,
  verbal confirmation stage, sensors used and last seen.
- Sector: compact 360° view only for a fresh validated external direction. Its
  empty state occupies the same space and names the missing prerequisite.
- Recommendation: one strong short action and one quieter detailed explanation.
- Events: at most five important items with time, operator label and short
  explanation.
- Readiness: seven fixed compact sensor tiles with reason and impact tooltips.
- Guided mode has one primary action. Technical identifiers, raw percentages,
  quality flags and evidence tables remain in Expert Mode.
- No wording may say that an aircraft type, nationality or hostile intent was
  established.

## Product signature

The footer always includes `Разработал: Буйвол и Задира`.
