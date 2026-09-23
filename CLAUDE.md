# CLAUDE.md

Guidance for Claude when working in this repository.

## What this repo is

The Cowrie honeypot on a DigitalOcean droplet, and the analysis of its logs.

`README.md` covers the layout and the data path. `analytics/README.md` covers
the bronze/silver/gold layers, how to query silver, the CLI, and the
environment variables. Read those for structure. This file only carries what
they do not.

Deployment lives in `homelab-services`: the Argo CD Applications, the Helm
charts, and the workflow definitions that say when each job runs. Kubernetes
manifests do not belong here.

## Writing style for any docs or comments you produce

The owner dislikes the typical AI documentation voice. When writing or editing
Markdown, comments, or commit messages, avoid the following:

- Bold for emphasis. If something matters, the sentence should carry it.
- Emoji in headings or bullets.
- Em dashes (`—`). Use a comma, a period, or parentheses.
- Empty intensifiers: `seamlessly`, `robust`, `powerful`, `comprehensive`,
  `elegant`, `modern`, `leverage`, `simply`, `easily`.
- Self-referential intros: "This document describes...", "In this section we
  will...".
- Meta-hedges: "It's worth noting that...", "It's important to understand
  that...".
- "Not just X, but Y" constructions.
- Decorative summary sentences at the end of every section.
- Forced parallelism: don't pad lists to 3 or 5 items, don't make every bullet
  `Term: description`, don't give every section the same depth.
- Pre-code throat-clearing ("Here is an example:", "The following snippet
  demonstrates:") and post-code recap ("As you can see...").
- Horizontal rules (`---`) used as decoration.

What to do instead: write short declarative sentences, let structure be uneven
where the content is uneven, and prefer concrete nouns to abstract praise. Aim
for a personal-notes tone, not a product page.

## Notebooks

They live in `analytics/notebooks/`. Commit them with their output. Source that
nobody can run is not worth reading, and without the bronze logs nobody can:
the output is the only part a reader gets.

That means being deliberate about what the output shows. Attacker side data is
fine to publish. Source addresses, the credentials they tried and the commands
they ran are exactly what a reader came for, and every honeypot feed publishes
them already.

Our own side is not. `dst_ip` is the droplet's public address and it rides along
in every raw event, so a cell that displays raw rows puts it in a public
repository under a real name. Drop it, or select columns rather than reaching
for `SELECT *`, in any cell whose output gets committed. The same goes for
anything else that identifies the host rather than the attacker.

## Releasing the analytics image

CI publishes on a tag. Tag `analytics-vX.Y.Z`, then bump the image in
`homelab-services` in two places: `charts/honeypot-jupyter/values.yaml` and
`charts/honeypot-pipeline/values.yaml`. Jupyter and the jobs deliberately run
the same image, so leaving one behind means they drift.
