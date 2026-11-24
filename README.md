# haplorecon — reassembling two latent sequences from sparse fragments

`haplorecon` reconstructs **two latent binary sequences** (haplotypes) from a
set of short **fragments** that each cover only a few positions. Which sequence
a fragment came from is a **hidden variable per fragment**, so every observation
in a fragment is scored *together* — never column by column. Some calls are
wrong, and the optimiser pays for them. Regions where the data cannot decide the
answer are reported as **undetermined** rather than guessed.

> **Synthetic data only.** Every fixture, sample and test instance is
> artificial 0/1 data written for this project. The application never reads,
> derives, or represents real human genetic data, and the sequences do not
> encode any locus, chromosome, or individual. Import your own **synthetic**
> fragments; do not paste real genetic records.

---

## Quick start

Standard library only — no dependencies, no build step. Python 3.9+ and
(optionally) Node.js for the headless UI test.

```bash
cd /workspace

# 1. run the web application
python3 -m haplorecon serve --port 8765
#    then open http://127.0.0.1:8765/

# 2. solve a fragment file from the command line
python3 -m haplorecon solve examples/two_blocks.txt

# 3. summarise every bundled sample
python3 -m haplorecon demo

# 4. run the tests
cd tests && python3 -m unittest test_model test_solver test_api test_ui
```

The database defaults to `haplorecon.sqlite3` in the working directory; use
`--db FILE` or `HAPLORECON_DB` to put it elsewhere. `*.sqlite3` (with its WAL
and SHM siblings) is ignored by git.

### Input format

Two accepted layouts, which can't be mixed in one file:

```
# FASTA-like
>f0 start=0
0110?1
>f1 start=4
1001

# TSV: id <TAB> start <TAB> sequence
f0	0	0110?1
f1	4	1001
```

`?`, `.`, `-`, and `N` mark a **missing** call and are skipped. A comment line
starts with `#`. Every fragment id must be unique.

---

## What problem is being solved

Let there be a position axis and two haplotype strings `h0`, `h1` over it.
Each fragment `f` covers a set of positions and carries observed calls
`a_f(p) ∈ {0,1}`; its hidden variable is `s_f ∈ {0,1}`, the haplotype it came
from. For a fixed orientation vector `s`:

```
cost(s; h0, h1) = Σ_p Σ_{f ∋ p} [ a_f(p) ≠ h_{s_f}(p) ]
```

Position costs are **independent once `s` is fixed**, so the optimum for a given
`s` is obtained by choosing, at each position, the allele pair `(h0(p), h1(p))`
that minimises that position's mismatches. The remaining problem is to choose
`s` — one bit per fragment, used at *all* of that fragment's positions.

**Why per-column voting fails.** Consider `a = 1111`, `b = 0000`, `e = 1110`.
A column vote gives `1110 / 0001`, which pays 2 against the fragments. The joint
optimum puts `e` entirely on one side and pays **1**: the whole fragment is one
decision, so evidence at its good calls constrains how its bad call is charged.

### Model properties

| Property | Behaviour |
| --- | --- |
| Objective | Integer error count. All observations of a fragment scored jointly. |
| `h0`/`h1` order | A free global swap unless a lock pins an allele to one label. |
| Coverage components | Positions joined by shared fragments form one component; each is solved separately. Components are **never stitched** into one certain sequence — the output keeps them as blocks. |
| Gaps | Coordinate ranges no fragment observes are reported as `gap` undetermined regions. |
| Per-component flip | Each component may be independently label-swapped unless a lock pins its order. |
| Locks | Hard constraints: pin `hap0`, pin `hap1`, and/or require the pair to differ. Contradictions are reported with the conflicting fragments. |

---

## Solving engines

The engine chosen by `auto` for each component, in order:

1. **`sweep-dp`** — exact dynamic programme over the position axis. The state at
   a cut is the orientation of the fragments still *alive* there (started but
   not finished). Sparse local coverage keeps this state tiny, so this handles
   large instances exactly. It also returns the **exact** number of optimal
   orientation vectors, per-position allele-pair option sets, and a bounded
   list of optimal reconstructions. Caps: 32 768 live states and 6 M
   transitions.
2. **`enumeration`** — Gray-code walk over all `2^n` orientation vectors for
   components with `n ≤ 18`; exhaustive, so the optimum is proven.
3. **`branch-and-bound`** — fallback for dense overlap. Starts from a local
   search incumbent, prunes with an admissible per-position bound, and computes
   a **proven additive lower bound** over position windows of 12. If the node
   or time budget runs out the status is `timeout`, with `lower_bound` and
   `upper_bound` reported and the result **never** presented as an optimum.

### Exact vs. bounded results

Small instances are exhaustive (enumeration/DP) and the result says
`status: "optimal"`, `proven: true`, with `lower_bound == cost`. On larger
instances the DP usually still succeeds. When it doesn't:

```json
{
  "status": "timeout",
  "proven": false,
  "cost": 27,                  // best solution found — an upper bound
  "lower_bound": 24,
  "upper_bound": 27,
  "timed_out_components": [1, 6]
}
```

Only components that ran out of budget appear in `timed_out_components`. Locked
positions contribute their own lower bound (`lock_lower_bound`), so a
contradictory lock is never hidden inside an unproven cost.

### Counting equivalent solutions

Each component's optimal set is a product over its positions of the allele pairs
that attain the per-position minimum, for each optimal orientation vector. The
global optimum count is the **product over components**, then divided by the
global `h0`/`h1` swap when no lock fixes the labels. Reconstructions that are
their own swap (`h0 == h1` at every position) are counted separately, so the
division uses Burnside:

```
n_solutions_modulo_labels = (raw_classes_product + palindromic_reconstructions) // 2
```

Component counts are exact while the enumeration stays under 400 000 pair
combinations; beyond that the count is reported as unknown rather than guessed.
At most 24 representative solutions are listed (`representatives_capped` says
whether that is the whole set), and `per_component_flip` names the blocks that
can be swapped independently.

---

## Incremental edits, versions, conflicts

The browser app and the API share one model: every change appends an **immutable
input version**, runs a **solve job**, and appends an **immutable result
version**.

* **Add a connector fragment.** A new fragment may join two components. The
  job detects the merge, re-solves the merged component as a whole, and
  reuses every untouched component from a cache keyed by
  `sha256(positions | fragment observations | locks)`. The report names the
  merge (“components 0, 1 -> 0”).
* **Pin a position.** Locks become hard constraints. A lock that pins `hap0` or
  `hap1` also removes the free global flip. If a fragment cannot satisfy the
  locks it touches, it is listed under `conflicts.unavoidable` with the
  positions that disagree; locks whose current reconstruction pays an error are
  listed with their `conflict_fragments`.
* **Compare.** Two listed solutions can be compared position by position, and
  the comparator flags when every difference is an exact `h0`/`h1` swap. Two
  result versions can be compared for cost, block count, and per-position
  changes.

---

## The web UI

`python3 -m haplorecon serve` then open the printed URL. The page is a
vanilla-JS single page (no framework, no bundler) with three tabs:

**Data** — paste fragments (FASTA or TSV), pick an engine and per-component time
budget, or start from a bundled sample. Recent datasets are listed with their
latest status and cost.

**Reconstruct**

* **Coverage map** (Canvas 2D): one row per fragment, coloured by assigned side,
  red where the call is an error, dashed yellow for locks, gap ranges shaded,
  and a `hap0`/`hap1` track with a flip-free marker. Zoom with `+` / `−`.
* **Candidate sequences**: `hap0` and `hap1` per block. `-` means no observation
  at that coordinate; blocks are drawn separately and never stitched.
* **Per-fragment assignments and errors**: side, span, calls, the exact error
  positions, and a call-vs-haplotype comparison strip. Filters for errors,
  conflicts, and connectors.
* **Undetermined regions**: `flip` (a block whose labels may swap), `gap` (no
  observations), `position_pairs` (more than one allele pair is optimal), and
  `unproven` (budget exhausted).
* **Conflict report**, **equivalence classes**, **solution comparison**,
  **hints**, and the **job log** (what each action recomputed and why).
* **Solve controls**, **add connector**, and **pin position** forms, with the
  current lock list.

**Versions** — every input version and result version, a compare tool, and
downloads for the input text and the result JSON.

---

## HTTP API

`GET /` and `GET /static/<file>` serve the UI. JSON routes:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/samples` | list bundled synthetic fixtures |
| GET | `/api/samples/<name>` | fixture text and expectation |
| GET | `/api/samples/<name>/download` | fixture as a text file |
| GET | `/api/datasets` | datasets with their latest result |
| POST | `/api/import` | `{text, name?, notes?, options?}` → new dataset + version 1 |
| GET | `/api/datasets/<id>` | current input version and the version list |
| GET | `/api/datasets/<id>/jobs` | job log, newest first |
| GET | `/api/datasets/<id>/results` | result version list |
| GET | `/api/datasets/<id>/results/<v>` | full result payload (result + dataset + job) |
| POST | `/api/datasets/<id>/connectors` | `{fid, alleles: {"4": 0, ...}}` |
| POST | `/api/datasets/<id>/locks` | `{position, hap0?, hap1?, distinct?}` |
| POST | `/api/datasets/<id>/unlock` | `{position}` |
| POST | `/api/datasets/<id>/resolve` | re-solve with new `options` |
| GET | `/api/datasets/<id>/hints` | gap and flip-resolution suggestions |
| GET | `/api/datasets/<id>/compare?result=&a=&b=` | compare two listed solutions |
| GET | `/api/datasets/<id>/versions/compare?a=&b=` | compare result versions |
| GET | `/api/datasets/<id>/export/input` | input text download |
| GET | `/api/datasets/<id>/export/result?version=` | result JSON download |

Solve options: `{"engine": "auto"|"enum"|"bandb", "time_limit_s": 20,
"max_nodes": 300000, "max_variants": 200, "include_phase": true}`.
Time limits must be positive (clamped to 600 s).

Errors are JSON `{"error": "<kind>", "message": "..."}` with status 400 for
bad input, 404 for unknown routes/datasets, and 405 for a wrong method.

---

## Layout

```
haplorecon/
  model.py         fragments, locks, parsing, text/JSON round trips
  samples.py       bundled fixtures (loaded from examples/)
  solver/
    base.py        lock compilation, union-find components, scoring tables
    component.py   per-component engines, solution classes, branch and bound
    dp.py          exact sweep DP (optimum, count, option sets, variants)
    solve.py       dataset assembly, equivalence counting, conflicts, gaps
    jobs.py        incremental jobs and the component cache
  service.py       use cases: import, connector, lock, unlock, resolve, compare
  store.py         SQLite: datasets, input versions, jobs, result versions
  server.py        WSGI app: JSON API + static UI (stdlib wsgiref)
  static/          index.html, app.css, app.js
  __main__.py      CLI: serve / solve / samples / export / demo
examples/          synthetic fixtures (also exposed as samples)
scripts/
  make_samples.py  regenerates the fixtures
tests/
  helpers.py       independent brute-force reference (shares no solver code)
  test_model.py    parsing, locks, serialisation, coverage
  test_solver.py   DP vs brute force, costs, equivalence, locks, bounds
  test_api.py      HTTP end-to-end against a spawned server
  test_ui.py       headless UI flow through the real app.js
  ui_runner.js     DOM-shim runner; ui_flow.js is the probe it executes
  domshim.js       the DOM subset the UI uses (throws on anything unsupported)
```

---

## Testing

```bash
cd tests
python3 -m unittest test_model test_solver test_api test_ui -v
```

* `test_solver` compares the solver against `helpers.reference_solve`, an
  independent brute force that enumerates orientation vectors and allele pairs
  from the observations. Cost, the number of reconstructions, and every listed
  solution are checked — not just the objective value.
* `test_api` spawns the real server on an ephemeral port with a temporary
  SQLite file and drives every route with `urllib`.
* `test_ui` runs the real `index.html` and `app.js` inside `tests/domshim.js`
  (there is no browser here) and asserts on the DOM the UI actually built:
  coverage canvas operations, candidate-sequence blocks, fragment rows,
  connector merge reporting, lock rows, version lists, and export links.
  `domshim.js` throws on unsupported DOM usage, so a passing UI test means the
  script's real DOM calls work.

The brute-force reference also covered 400+ randomised instances with locks in
development; a representative sample of them runs in `test_random_instances`.

---

## Known limits

* Two haplotypes only; fragments are binary calls with missing entries.
* Coverage must be resolved by the engines above: dense overlap beyond the DP
  state/transition caps falls back to bounded branch-and-bound.
* Timestamps have one-second resolution; job ordering uses insertion order, so
  jobs created within the same second still display newest first.
* The web UI is a demo-scale tool: datasets are capped at 20 000 positions and
  5 000 fragments.
