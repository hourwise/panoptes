# The PCGsoft portfolio manifest

`.pcgsoft/project.yml` is the one file in this repository that describes Panoptes
to the outside world in machine-readable form. It and the two marker sections at
the top of `README.md` are written for a reader who has never seen this checkout,
which is why they are governed separately from the rest of the documentation.

## What it is

A **proposal**. PCGsoft runs a portfolio of projects; a project's repository may
carry `.pcgsoft/project.yml` to propose its own identity — slug, name, category,
lifecycle status, one-sentence summary, whether it has a live site, what role the
repository plays. PCGsoft's portfolio tooling reads those files, compares each
field with its own records and reports agreements, conflicts and unknowns.

- The canonical record — the one that decides what PCGsoft actually says about
  Panoptes publicly — lives in **PCGsoft's own site repository**, is maintained by
  hand and is changed only through a reviewed change approved by a human.
- A manifest grants nothing. `publication.eligible` and `activity.enabled` here
  are informational: they cannot publish, promote or enrol anything, whatever
  value they hold.
- Nothing in this repository publishes anything. The portfolio entry for Panoptes
  exists only after a person has approved it on the PCGsoft side.

So this file is a courteous, accurate statement of what the project is, that
someone else may choose to adopt. It is not authority, and it is not a channel:
it never leaves the repository by itself.

The schema it follows is PCGsoft's **version 2**: a small safe YAML subset (no
anchors, aliases, tags or multi-line values), a fixed set of keys — an unknown key
makes the file invalid rather than ignored — bounded string lengths, a 24 KB size
cap, HTTPS-only URLs, and lifecycle, web-state and repository-role values from
fixed enums.

## The README markers

The two bounded sections in `README.md`, delimited by

```
<!-- pcgsoft:overview:start -->   …   <!-- pcgsoft:overview:end -->
<!-- pcgsoft:status-notes:start -->   …   <!-- pcgsoft:status-notes:end -->
```

are the prose counterpart of the manifest: the sync reads exactly those, each up
to 4,000 characters, nothing else from the README.

- **`pcgsoft:overview`** — what Panoptes *is and does today*. Implemented
  behaviour only, in the same words the feature list uses.
- **`pcgsoft:status-notes`** — the development status, and the **roadmap kept
  clearly separate** from what ships. A reader must not be able to mistake
  unfinished work for a feature.

The rest of the README, and `docs/`, stay the human-facing documentation: the
markers summarise them and link to them, they do not restate them.

## When to review the manifest and the markers

At points where the public description could genuinely have changed:

- a **milestone** or a **release** — the version, and the release checklist in
  [RELEASE.md](RELEASE.md), are the trigger;
- a **new capability** that is actually shipped: a second harness, a supported
  platform, Guardian, headless multi-GPU discovery;
- a **status change**: public preview, live, maintenance, paused, archived;
- a **change of fact** in the file itself: a first live site (which would move
  `web.state` and `web.urls`), a rename, a new parent or related project;
- before PCGsoft reviews or registers the project, so the proposal matches the
  tree it describes.

Then re-read all three texts together — `project.yml`, `README.md`'s two marked
sections — against what the tree really does, and update `project.lastReviewed`.

## When not to

Ordinary work does not touch them:

- routine commits, refactors, dependency-free housekeeping;
- bug fixes, including fixes to shipped features, unless the fix changes what
  those features may be described as doing;
- test, tooling or documentation changes that leave the public description true;
- roadmap churn: an idea added to the roadmap does not belong in `project.yml`,
  and stays in `pcgsoft:status-notes` only if it is a real plan worth telling
  people about.

The test is one question: *would a stranger reading these three texts now be
better or worse informed than before?* If neither, leave them alone — a manifest
edited in every commit is a manifest nobody trusts.

## What may never go in them

Never in `.pcgsoft/project.yml`, and never in the two marker sections:

- hostnames, `*.local` names, or IP addresses of any kind;
- local filesystem paths, home directories, or the location of anything on this
  machine;
- usernames, login names, e-mail addresses or credentials;
- private repository names, private organisation identifiers, or any mention of
  the development checkout this project is written in;
- task packets, receipts, tickets, internal notes or log excerpts;
- dashboard URLs, ports or endpoints of a running instance;
- roadmap items phrased as facts, or figures that the code does not actually
  produce.

`python3 tools/release_scan.py` enforces the machine-specific half of that list
over the whole product set, including `.pcgsoft/`; the rest — a claim that is
true in spirit but not in the tree — is what the review above is for.

## Validating them

```sh
python3 -m unittest tests.test_pcgsoft_manifest -q   # schema, values, markers
python3 tools/release_scan.py                        # nothing machine-specific
```

The manifest test checks the file against a table of the version-2 keys and enums
and checks both README marker sections for balance and length. It also runs
**PCGsoft's own validator**, offline, when both are available: a local read-only
clone of the PCGsoft site repository and `node` on `PATH`. The clone is looked for
at `~/Projects/PCGsoft/panoptes-integration`, or wherever `PCGSOFT_SITE` points;
with neither present the check skips, so a public checkout passes on its own.
The validation is read-only — the site repository is never edited, committed to
or pushed to from here.

To run the site validator by hand from a clone of the site repository, with the
absolute path of this file:

```sh
node --input-type=module -e 'const s=await import("./scripts/portfolio-sync-lib.mjs");const r=await import("./scripts/portfolio-reconcile.mjs");const fs=await import("node:fs");const t=fs.readFileSync(process.argv[1],"utf8");const c=await r.parseManifestContent(t);console.log(c.schemaVersion,c.declaredProject,JSON.stringify(c.errors));' /absolute/path/to/.pcgsoft/project.yml
```

`scripts/portfolio-sync-lib.mjs` holds the safe YAML parser and `validateManifest`
for schema version 1; `scripts/portfolio-reconcile.mjs` holds `validateManifestV2`
and `parseManifestContent`, which is the path the sync itself uses and which
dispatches on `schemaVersion`. `parseManifestContent` reporting no errors is the
result to look for; `validateManifest` on its own is the version-1 validator and
must reject a version-2 file.
