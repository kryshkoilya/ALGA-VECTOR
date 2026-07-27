# Superdesign handoff

## Approved base

- Project canvas:
  <https://superdesign.dev/teams/504a14f5-5dd0-448d-a4b0-3b24a4edf5bc/projects/71600833-25c3-4a63-bb7b-3b9a1f8b4b87?live=1>
- Dashboard source:
  [ALGA VECTOR — Операционный интерфейс (Navigation Refinement)](https://p.superdesign.dev/draft/b5f56a47-fe71-4bc6-ab2e-bab0f0af4edf)
- Reusable navigation component:
  [MainNavigation](https://p.superdesign.dev/draftcomponent/c987c333-acfa-48a4-9b5a-bbbda7a90df0)

The base was browser-rendered at 1440×900. It uses a readable 112 px Russian navigation rail,
Golos Text, flat charcoal surfaces, a dominant spectrum canvas, no gradients/colored glow, and
keeps every unavailable capability visibly separate from the working spectrum path.

## v1.0 target-centric refinement

The approved dashboard remains the visual base. The executable product applies its tokens and
navigation rules to the working routes: Simple Situation, overview, expert Targets, devices,
spectrum, generic RF events, Direction, expert map, diagnostics, settings, onboarding and
training.

The SIMPLE MODE direction surface is a compact 360-degree sector presentation. It stays
unavailable unless a validated external DF source supplies fresh calibrated evidence. It never
shows coordinates, range rings or inferred distance. The existing map remains an EXPERT-only
context page and cannot derive a target position from one bearing.

Older sibling drafts, including the archived situation-map concept, are historical references
and are historical references. The v1.0 local design system is authoritative:
[`../.superdesign/design-system.md`](../.superdesign/design-system.md).

The final Superdesign cloud regeneration was attempted after the v1.0 prompt update but the
remote API repeatedly terminated the connection (`ECONNRESET`). No unverified cloud result was
substituted. The implementation therefore uses the approved base draft plus the reviewed local
design system and native PySide6 visual QA.

## Implemented UI QA

The local QA pass:

- keeps SIMPLE MODE to one hero decision surface, at most one current target
  with an honest empty state, and one next action;
- uses verbal confirmation stages without visible percentages;
- adds a compact validated sector and explicit no-bearing state;
- shows all seven canonical sensor-readiness roles with reason and impact;
- retains the complete evidence chain in Events and Expert mode;
- normalizes operational text to at least 12 px;
- removes gradients and colored glow;
- adds explicit empty, unavailable, degraded and failure states;
- separates measured RF facts from heuristic interpretation;
- replaces non-working controls with capability-aware, fail-closed states;
- keeps map/GPS outside SIMPLE MODE and never derives target location from them.
