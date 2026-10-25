#!/usr/bin/env python3
"""
export_series_parents.py - list the character -> series tag parent pairs found in your Camie scan results.

In Hydrus, make each character tag a CHILD of its series tag:
    character:hu tao (genshin impact)   ->   series:genshin impact
After that, a search for series:genshin impact also finds every image of a Genshin character, even when Camie did not
report the series on that particular image. Crossover art works as you'd hope: each character in the picture adds its
own series, so an image with Hu Tao and Shiroko ends up with both series.

The series for a character is chosen in this order:
  1. the name in its brackets, e.g. "shiroko (blue archive)" -> series:blue archive, but only if Camie has really
     reported a series with that exact name on your images (this avoids made-up series tags);
  2. otherwise the series Camie reports on at least MIN_SHARE of that character's images (needs MIN_IMAGES images).
Characters that match neither are not listed in the import file. They are written to a separate CSV so you can look at
them (for example Hatsune Miku, who belongs to several series).

Pairs come out in three groups: "safe" (series named in the brackets), "likely" (guesses backed by at least
STRONG_IMAGES images with STRONG_SHARE on one series) and "check" (weaker guesses, kept only in the CSV). Only safe and
likely get an import file, so a normal run means importing one file, then skimming the other.

It only reads the score cache and writes files; it never contacts Hydrus. Uses the same character names, aliases and
costume folding as the push, and only characters at or above character_threshold in hydrus.json.

    python export_series_parents.py     # writes data/tag_parents_series.txt, ..._likely.txt, .csv and an unmatched list

Hydrus: tags > manage tag parents > pick the service that holds the tags (camie) > import > from .txt file or
clipboard. CHECK THE LIST IN THE DIALOG BEFORE CLICKING APPLY: the character must be the CHILD (left) and the series the
PARENT (right), the same layout as the costume file from export_parents.py.
"""
import csv
import json
import os
import re
from collections import Counter, defaultdict

import char_sorter as cs
import export_parents as ep
import push_to_hydrus as ph

MIN_IMAGES = 3      # a character needs this many images before its series is guessed from co-occurrence
MIN_SHARE = 0.8     # ...and this share of them must carry the same series
STRONG_IMAGES = 10  # a guess goes in the "likely" import file only with at least this many images...
STRONG_SHARE = 0.9  # ...and at least this share carrying the same series


def collect(c, terms):
    """Return (images per character, series counts per character, every series name Camie reported)."""
    con = cs.db_connect()
    series_by_file = {}
    for base, top_json in con.execute("SELECT name, top FROM series_scores"):
        names = {ph.clean(n) for n, score in json.loads(top_json or "[]") if score >= c["series_threshold"]}
        if names:
            series_by_file[base] = names
    images = Counter()
    with_series = defaultdict(Counter)
    for path, top_json in con.execute("SELECT path, top FROM scores"):
        base = os.path.basename(path)
        who_here = set()
        for name, score in json.loads(top_json or "[]"):
            if score < c["character_threshold"]:
                continue
            canonical, _costume = ph.split_character_costume(name, terms)
            canonical = cs.ALIASES.get(canonical, canonical)
            who_here.add(ph.clean(canonical))
        for who in who_here:
            images[who] += 1
            for s in series_by_file.get(base, ()):
                with_series[who][s] += 1
    con.close()
    all_series = {s for names in series_by_file.values() for s in names}
    return images, with_series, all_series


def bracket_name(who):
    m = re.search(r"\(([^()]*)\)\s*$", who)
    return ph.clean(m.group(1)) if m else None


def choose_series(who, n_images, counts, all_series):
    """Return (series, how, share) or (None, reason, 0)."""
    inside = bracket_name(who)
    if inside and inside in all_series:
        return inside, "brackets", 1.0
    if counts and n_images >= MIN_IMAGES:
        series, k = counts.most_common(1)[0]
        if k / n_images >= MIN_SHARE:
            return series, "most images", k / n_images
        return None, f"no single series (best: {series}, {k} of {n_images} images)", 0
    if inside:
        return None, f"'{inside}' is not a series Camie reported", 0
    return None, "no series seen on its images" if n_images >= MIN_IMAGES else "too few images", 0


def main():
    c = ph.load_config()
    terms = ep.load_terms(c)
    images, with_series, all_series = collect(c, terms)
    if not images:
        print("No characters at or above character_threshold in the cache. Run the tagger first.")
        return
    if not all_series:
        print("Camie has not reported any series yet (needs tagger_backend camie and series_from_copyright). "
              "Only bracket names could be checked, so nothing can be listed.")
        return

    pairs, unmatched = [], []
    for who in sorted(images):
        series, how, share = choose_series(who, images[who], with_series[who], all_series)
        if series:
            if how == "brackets":
                group = "safe"
            elif images[who] >= STRONG_IMAGES and share >= STRONG_SHARE:
                group = "likely"
            else:
                group = "check"
            pairs.append(("character:" + who, "series:" + series, images[who], how, group))
        else:
            unmatched.append((who, images[who], how))

    os.makedirs(cs.DATA_DIR, exist_ok=True)
    txt = os.path.join(cs.DATA_DIR, "tag_parents_series.txt")                  # safe: series named in the brackets
    txt_likely = os.path.join(cs.DATA_DIR, "tag_parents_series_likely.txt")    # guesses backed by lots of images
    csv_path = os.path.join(cs.DATA_DIR, "tag_parents_series.csv")
    un_path = os.path.join(cs.DATA_DIR, "tag_parents_series_unmatched.csv")
    for path, group in ((txt, "safe"), (txt_likely, "likely")):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for child, parent, _n, _how, g in pairs:
                if g == group:
                    f.write(child + "\n" + parent + "\n")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["child (character)", "parent (series)", "images", "how the series was found", "group"])
        w.writerows(sorted(pairs, key=lambda p: (p[4], -p[2])))
    with open(un_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["character", "images", "why no series"])
        w.writerows(sorted(unmatched, key=lambda u: -u[1]))

    groups = Counter(p[4] for p in pairs)
    print(f"{len(pairs)} character -> series pairs: {groups['safe']} safe, {groups['likely']} likely, "
          f"{groups['check']} to check. {len(unmatched)} characters left out.")
    for child, parent, n, how, _g in sorted(pairs, key=lambda p: -p[2])[:8]:
        print(f"  {child}   ->   {parent}   ({n} images, {how})")
    print(f"\nImport these:\n  {txt}   (safe: series named in the brackets)\n"
          f"  {txt_likely}   (likely: guesses backed by lots of images; skim first)\n"
          f"Only for reading:\n  {csv_path}   (every pair, with the group of each)\n"
          f"  {un_path}   (characters with no clear series; usually too few images)\n"
          f"The {groups['check']} pairs marked 'check' are in the CSV only and are not in either import file.")


if __name__ == "__main__":
    main()
