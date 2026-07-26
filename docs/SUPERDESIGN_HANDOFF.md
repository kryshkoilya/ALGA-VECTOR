# Superdesign handoff

## Approved base

- Project canvas:
  <https://superdesign.dev/teams/504a14f5-5dd0-448d-a4b0-3b24a4edf5bc/projects/070180e9-d3e3-4086-8a6b-28390148fe38>
- Dashboard source:
  [ALGA VECTOR — Операционный интерфейс (Navigation Refinement)](https://p.superdesign.dev/draft/b5f56a47-fe71-4bc6-ab2e-bab0f0af4edf)

The base was browser-rendered at 1440×900. It uses a readable 112 px Russian navigation rail,
Golos Text, flat charcoal surfaces, a dominant spectrum canvas, no gradients/colored glow, and
keeps every unavailable capability visibly separate from the working spectrum path.

## v0.5.0 production refinement

The approved dashboard remains the visual base. The executable product applies its tokens and
navigation rules to these working routes only: overview, devices, spectrum, generic RF events,
Direction, diagnostics, settings, onboarding and training.

Direction is not a map. It is a 360-degree bearing presentation which stays unavailable unless
manual input or a validated external DF source supplies fresh, calibrated evidence. It never
shows coordinates, range rings or inferred distance.

Older sibling drafts, including the archived situation-map concept, are historical references
and are not current product requirements. The v0.5.0 local design system is authoritative:
[`../.superdesign/design-system.md`](../.superdesign/design-system.md).

The final Superdesign cloud regeneration was attempted after the v0.5.0 prompt update but the
remote API repeatedly terminated the connection (`ECONNRESET`). No unverified cloud result was
substituted. The implementation therefore uses the approved base draft plus the reviewed local
design system.

## Implemented UI QA

The local QA pass:

- keeps the novice overview to one decision surface and one next action;
- retains the complete evidence chain in Events and Expert mode;
- normalizes operational text to at least 12 px;
- removes gradients and colored glow;
- adds explicit empty, unavailable, degraded and failure states;
- separates measured RF facts from heuristic interpretation;
- replaces non-working controls with capability-aware, fail-closed states;
- removes map/GPS from visible navigation and onboarding.
