"""Generate the bundled synthetic fixtures in ``examples/``.

Run: ``python3 scripts/make_samples.py``

Everything produced here is pseudorandom binary data written for this project.
No real or derived human genetic data is used or representable: the sequences
are abstract 0/1 tokens with no locus, chromosome, or individual attached.
"""
from __future__ import annotations

import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "examples")


def frag_lines(frags):
    lines = []
    for fid, alleles in frags:
        lo, hi = min(alleles), max(alleles)
        lines.append(f">{fid} start={lo}")
        lines.append("".join(str(alleles[p]) if p in alleles else "?" for p in range(lo, hi + 1)))
    return "\n".join(lines) + "\n"


def write(name, header, frags):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{name}.txt"), "w", encoding="utf-8") as fh:
        fh.write(header + "\n" + frag_lines(frags))


def two_blocks():
    h0 = "0110100111"
    h1 = "1001011001"
    frags = []
    for i, (s, side, length) in enumerate([(0, 0, 3), (1, 1, 3), (2, 0, 2), (0, 1, 4), (3, 0, 2)]):
        seq = h0 if side == 0 else h1
        frags.append((f"b{i}", {s + j: int(seq[s + j]) for j in range(length)}))
    for i, (s, side, length) in enumerate([(7, 0, 3), (8, 1, 2), (6, 0, 3), (7, 1, 2)]):
        seq = h0 if side == 0 else h1
        frags.append((f"c{i}", {s + j: int(seq[s + j]) for j in range(length)}))
    write(
        "two_blocks",
        "# synthetic fixture: two coverage blocks separated by a gap at positions 5-6\n"
        "# every value is artificial binary data written for this demo",
        frags,
    )


def single_error():
    rng = random.Random(11)
    h0 = "0011010011"
    h1 = "1100101100"
    frags = []
    for i in range(10):
        s = rng.randint(0, 7)
        side = rng.randint(0, 1)
        seq = h0 if side == 0 else h1
        length = min(3, len(h0) - s)
        alleles = {s + j: int(seq[s + j]) for j in range(length)}
        if i == 4:
            p = sorted(alleles)[1]
            alleles[p] ^= 1  # the one injected read error
        frags.append((f"f{i}", alleles))
    write(
        "single_error",
        "# synthetic fixture: ten fragments, exactly one flipped call (in f4)\n"
        "# every value is artificial binary data written for this demo",
        frags,
    )


def lock_conflict():
    # x1 runs across the whole span and carries opposite haplotypes at its two
    # ends.  Locking position 0 to hap0=1 and position 6 to hap0=0 therefore
    # makes it impossible for x1 to be consistently assigned, and the report
    # must name x1 rather than silently paying the cost elsewhere.
    # h0 = 101010101, h1 = 010101010.  x1 is a *chimera*: its 0..5 half comes
    # from h0, its position-6 call comes from h1.  Locking position 0 and
    # position 6 with the full pair (hap0/hap1 pinned) makes x1 impossible to
    # orient, so it must be reported as an unavoidable conflict.
    frags = [
        ("x0", {0: 1, 1: 0, 2: 1, 3: 0}),
        ("x1", {0: 1, 1: 0, 2: 1, 3: 0, 4: 1, 5: 0, 6: 0}),
        ("x2", {2: 1, 3: 0, 4: 1}),
        ("x3", {3: 0, 4: 1, 5: 0}),
        ("x4", {4: 1, 5: 0, 6: 1, 7: 0}),
        ("x5", {5: 0, 6: 1, 7: 0, 8: 1}),
        ("x6", {6: 0, 7: 1, 8: 0}),
    ]
    write(
        "lock_conflict",
        "# synthetic fixture: x1 is chimeric (h0 for 0..5, h1 at position 6)\n"
        "# lock p0 and p6 with hap0=1,hap1=0 to make x1 an unavoidable conflict\n"
        "# every value is artificial binary data written for this demo",
        frags,
    )


def merge_demo():
    h0 = "0101101001"
    h1 = "1010010110"
    frags = []
    for i, (s, side) in enumerate([(0, 0), (1, 1), (2, 0), (0, 1), (1, 0), (2, 1), (3, 0)]):
        seq = h0 if side == 0 else h1
        frags.append((f"a{i}", {s + j: int(seq[s + j]) for j in range(3)}))
    for i, (s, side) in enumerate([(6, 0), (7, 1), (8, 0), (6, 1), (7, 0), (8, 1)]):
        seq = h0 if side == 0 else h1
        length = min(3, len(h0) - s)
        frags.append((f"b{i}", {s + j: int(seq[s + j]) for j in range(length)}))
    write(
        "merge_demo",
        "# synthetic fixture: two blocks; add a connector such as {4:0,5:1}\n"
        "# every value is artificial binary data written for this demo",
        frags,
    )


def long_scaffold():
    rng = random.Random(2026)
    h0 = "".join(rng.choice("01") for _ in range(300))
    h1 = "".join(a if rng.random() < 0.6 else ("1" if a == "0" else "0") for a in h0)
    frags = []
    for i in range(90):
        s = rng.randint(0, 280)
        length = rng.randint(6, 16)
        seq = h0 if rng.random() < 0.5 else h1
        alleles = {}
        for j in range(length):
            p = s + j
            if p >= len(h0) or rng.random() < 0.03:
                continue
            a = int(seq[p])
            if rng.random() < 0.04:
                a ^= 1
            alleles[p] = a
        if alleles:
            frags.append((f"L{i:02d}", alleles))
    write(
        "long_scaffold",
        "# synthetic fixture: larger instance for bounds and timeout behaviour\n"
        "# every value is artificial binary data written for this demo",
        frags,
    )


def main():
    two_blocks()
    single_error()
    lock_conflict()
    merge_demo()
    long_scaffold()
    print(f"wrote fixtures to {OUT}")


if __name__ == "__main__":
    main()
