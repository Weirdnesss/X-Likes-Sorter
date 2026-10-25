#!/usr/bin/env python3
"""
export_parents.py - list the costume -> character tag parent pairs found in your Camie scan results.

Camie names costumes as separate characters ("kazama_iroha_(1st_costume)"). In Hydrus that becomes
    character:kazama iroha (1st costume)
which a search for  character:kazama iroha  does not find. A tag PARENT fixes that: make the costume tag a child of
the plain character and a search for the character also finds the costume images, while each image keeps showing the
costume tag. Nothing in the push changes; this script only reads the score cache and writes a list.

It uses costume_terms (config/costume_terms.json or a "costume_terms" list in hydrus.json) to decide which
parenthetical words are costumes, and the same character aliases as the sorter. Only characters that score at or
above character_threshold in hydrus.json are listed, because only those become character: tags.

    python export_parents.py            # writes data/tag_parents.txt and data/tag_parents.csv, prints a summary

Hydrus: tags > manage tag parents > pick the service that holds the tags (camie) > import > from .txt file or
clipboard. CHECK THE LIST IN THE DIALOG BEFORE CLICKING APPLY: the costume tag must be the CHILD (left) and the plain
character the PARENT (right). If they are the wrong way round, do not apply; tell me and I will flip the file.
"""
import csv
import json
import os
import re
from collections import Counter

import char_sorter as cs
import push_to_hydrus as ph


def split_character_costume(name, costume_terms):
    """Return (character tag without the costume part, costume or None). Same rules as the push."""
    terms = {re.sub(r"[\s_]+", "_", str(t).strip().lower()) for t in costume_terms}
    costume = None
    for m in re.finditer(r"_\(([^()]*)\)", name):
        variant = re.sub(r"[\s_]+", "_", m.group(1).strip().lower())
        if variant in terms:
            costume = variant
            name = name[:m.start()] + name[m.end():]
            break
    return re.sub(r"_+", "_", name).strip("_ "), costume


def load_terms(c):
    if c.get("costume_terms"):
        return c["costume_terms"]
    for p in (os.path.join(cs.CONFIG_DIR, "costume_terms.json"), "costume_terms.json"):
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig") as f:
                return json.load(f).get("costume_terms", [])
    return []


def find_pairs(c, terms):
    aliases = getattr(cs, "ALIASES", {}) or {}
    pairs = Counter()
    con = cs.db_connect()
    for (top_json,) in con.execute("SELECT top FROM scores"):
        for name, score in json.loads(top_json or "[]"):
            if score < c["character_threshold"]:
                continue
            canonical, costume = split_character_costume(name, terms)
            if not costume:
                continue
            canonical = aliases.get(canonical, canonical)
            child = "character:" + ph.clean(name)
            parent = "character:" + ph.clean(canonical)
            if child != parent:
                pairs[(child, parent)] += 1
    con.close()
    return pairs


def main():
    c = ph.load_config()
    terms = load_terms(c)
    if not terms:
        raise SystemExit("No costume_terms found (config/costume_terms.json or a list in hydrus.json).")
    pairs = find_pairs(c, terms)
    if not pairs:
        print("No costume tags found in the cache.")
        return
    os.makedirs(cs.DATA_DIR, exist_ok=True)
    txt = os.path.join(cs.DATA_DIR, "tag_parents.txt")
    csv_path = os.path.join(cs.DATA_DIR, "tag_parents.csv")
    ordered = sorted(pairs)
    with open(txt, "w", encoding="utf-8", newline="\n") as f:
        for child, parent in ordered:
            f.write(child + "\n" + parent + "\n")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["child (costume)", "parent (character)", "images"])
        for child, parent in ordered:
            w.writerow([child, parent, pairs[(child, parent)]])
    print(f"{len(ordered)} costume -> character pairs, covering {sum(pairs.values())} image tags.")
    for (child, parent), n in pairs.most_common(8):
        print(f"  {child}   ->   {parent}   ({n})")
    print(f"\nWrote {txt}\n      {csv_path}  (same pairs with counts, for reading)")


if __name__ == "__main__":
    main()
