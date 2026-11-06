---
title: 'Bearings AC-7d literature notes: detectability of a weaker planted split'
description: Pre-score notes on planted-partition detectability limits, framing how an AC-7d pass or fail will be read
status: active
task_id: 261104_multiscale-goal-awareness
date: '261106'
confidence: 'medium: analogy from network inference to blind LLM readers, not a measured transfer'
sources: 'arXiv 1109.3041 (Decelle et al. 2011, PRE 84 066106); arXiv 1608.00163 (Fortunato and Hric 2016, Phys. Rep. 659); Shi et al. 2019 as cited in the Bearings spec'
---
# Bearings AC-7d literature notes: detectability of a weaker planted split

Written 261106, after the readers ran and before labelling was applied or the scorer read anything.
Its only purpose is to fix, in advance, which prior work frames the reading of an AC-7d pass or fail.

## What AC-7d asks

AC-7c showed that blind readers split a packet along a strong planted seam: two projects with no
results flow and no shared domain. AC-7d plants a weaker seam: two neighbouring-field projects with
no results flow but a shared vocabulary, over a shorter window. The question is whether the
instrument still separates them, or whether the split falls below what it can detect.

## Prior work that frames it

- **Detectability limits in planted partitions.** In the stochastic block model, a planted split
  can be recovered only while the contrast between within-group and between-group links stays above
  a threshold. Below it, no method recovers the plant better than chance, even though the plant is
  real (Decelle, Krzakala, Moore and Zdeborova 2011, arXiv 1109.3041, Phys. Rev. E 84, 066106; the
  limit was first reported in a Phys. Rev. Lett. letter the same year). There is also a hard region
  where recovery is possible in principle but not by efficient methods.
- **Practitioner reading.** Fortunato and Hric (2016, Phys. Rep. 659, arXiv 1608.00163) stress that
  community detection is ill-defined without a model of what a group is, and that a method should be
  checked on planted benchmarks of graded difficulty, not on one easy plant.
- **Weak plants in synthetic work.** The Bearings spec already cites Shi et al. 2019 for an
  "undetectable" regime of weak plants.

## How this shapes the reading (fixed now, gates nothing)

- AC-7c alone is one easy benchmark. AC-7d is the graded step the practitioner literature asks for.
- A **fail** at m4 (median 1 group) with the anchor valid is a reading of "below this instrument's
  detection limit for a shared-vocabulary split", not evidence that the two projects are one effort.
  The registered fail reading (halves joined by cross-seam binding links on shared method, or not
  joined and still 1) decides which of the two it was.
- A **pass** shows the limit lies below this plant's contrast. It does not locate the limit, and it
  does not transfer to a split within one project (colliculix).
- The analogy is loose: readers are not a block-model inference method, and the "contrast" here is
  vocabulary and results flow, not edge densities. No threshold from this literature is applied.
