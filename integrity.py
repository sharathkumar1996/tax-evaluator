"""
integrity.py - checks for copying and other academic dishonesty.

Two halves:

1. ``fingerprints(files, ret)`` runs when a return is evaluated and records small, anonymous
   "fingerprints" of it (hashes, PDF properties, typing habits). Nothing else of the file is kept.

2. ``analyse(...)`` compares every pair of returns of the same case (across all batches) and
   explains, in plain words, what links them.

Evidence used (strongest first)
-------------------------------
File evidence - survives renaming the file and changing the student ID:
  * file        exactly the same PDF bytes
  * filled      one file contains the other's filled-in work: PDF viewers usually save changes by
                appending to the file, so a file saved over another student's file still carries
                that student's version inside it
  * save_id / xmp_instance
                the internal ID a PDF viewer writes when it saves; only used when the viewer
                changed it after the blank form was downloaded
  * doc_id      the original document ID of a printed PDF (only in returns marked before
                printed PDFs were refused)
  * meta        identical created + saved time stamps (to the second) and software
  * author      the computer user name in the file properties (printed PDFs, as above)
  * saved_at    saved within 2 minutes of each other (weak - could be the same lab session)
Answer evidence:
  * shared wrong answers: the same WRONG value on the same line. Weighted by rarity: a mistake
    many students make is a common misconception, not evidence; the same unusual figure is.
  * all_entries every box identical, including how numbers and names were typed
  * style_zero  typed "0" in exactly the same set of boxes (others leave them blank)
  * style_text  identical text entries, including capitalisation and spacing
Trainer evidence:
  * a student file built on the trainer's own correct return ("file" / "filled" match)

Template guard: values that every downloaded blank form carries (the IRS's own author, dates,
document IDs) are not evidence. They are ignored when the trainer's file has the same value, when
more than a third of the case's returns share them, or (save IDs) when the viewer never changed them.
"""
import hashlib
import io
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd
from pypdf import PdfReader

TRAINER = "#TRAINER"

LEVELS = [(90, "Very likely copied"), (60, "Likely copied"), (30, "Review")]
SIGNALS = {   # kind: (points, plain-language evidence)
    "file": (100, "Exactly the same PDF file"),
    "filled": (100, "One file contains the other's filled-in work (saved over another student's file)"),
    "save_id": (90, "Same PDF save ID - the same saved copy of the form"),
    "xmp_instance": (90, "Same PDF save ID (XMP) - the same saved copy of the form"),
    "doc_id": (60, "Same original PDF document ID - printed from the same file"),
    "xmp_document": (60, "Same original PDF document ID (XMP)"),
    "meta": (70, "Same created and saved time stamps, to the second, and same software"),
    "all_entries": (50, "Every box identical, including how numbers and names were typed"),
    "author": (25, "Same computer user name in the file properties"),
    "style_zero": (15, "Typed 0 in exactly the same boxes that others leave blank"),
    "style_text": (15, "Identical text entries, including capitalisation and spacing"),
}
FILE_KINDS = {"file", "filled", "save_id", "xmp_instance", "doc_id", "xmp_document", "meta", "author"}
TRAINER_KINDS = {"file", "filled"}              # content proof only; the rest may be template values
TRAINER_TEXT = {"file": "Exactly the trainer's correct-return PDF",
                "filled": "Built on the trainer's filled-in correct return"}
CLOSE_SAVE_SECONDS = 120
CLOSE_SAVE_POINTS = 10


def level(score):
    for cut, name in LEVELS:
        if score >= cut:
            return name
    return ""


def _h(b):
    if isinstance(b, str):
        b = b.encode("utf-8", "replace")
    return hashlib.sha256(b).hexdigest()[:32]


def _hexid(v):
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.hex()
    try:
        return bytes(v, "latin-1").hex()
    except Exception:
        return str(v)


def _pdf_date(s):
    """'D:20260102072431-06'00'' -> aware datetime (UTC) or None."""
    m = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Z+\-])?(\d{2})?'?(\d{2})?", str(s or ""))
    if not m:
        return None
    y, mo, d, hh, mi, ss_ = (int(x) if x else dflt for x, dflt in zip(m.groups()[:6], (0, 1, 1, 0, 0, 0)))
    try:
        dt = datetime(y, mo, d, hh, mi, ss_)
    except ValueError:
        return None
    sign, oh, om = m.group(7), int(m.group(8) or 0), int(m.group(9) or 0)
    off = timedelta(hours=oh, minutes=om)
    if sign == "+":
        dt -= off
    elif sign == "-":
        dt += off
    return dt.replace(tzinfo=timezone.utc)


def _ids(rd):
    out = {}
    try:
        tid = rd.trailer.get("/ID")
        if tid and len(tid) == 2:
            out["doc_id"], out["save_id"] = _hexid(tid[0]), _hexid(tid[1])
    except Exception:
        pass
    try:
        x = rd.xmp_metadata
        if x is not None:
            out["xmp_document"] = str(x.xmpmm_document_id or "")
            out["xmp_instance"] = str(x.xmpmm_instance_id or "")
    except Exception:
        pass
    return {k: v for k, v in out.items() if v}


def _filled(rd, max_pages=12):
    from irs_reader import _widgets
    try:
        for pg in rd.pages[:max_pages]:
            for w in _widgets(pg):
                if w.value not in ("", None, False):
                    return True
    except Exception:
        return False
    return False


def _revisions(data, limit=20):
    """Byte offsets just after each '%%EOF' (each save of an incrementally saved PDF)."""
    ends = [m.end() for m in re.finditer(rb"%%EOF", data)]
    return ends[-limit:]


# ------------------------------------------------------------------ 1. fingerprints of one return
def fingerprints(files, ret=None):
    """files: [(name, bytes)] of one return; ret: the reader's result. Returns [(kind, value, detail)]."""
    out = []
    fillable = getattr(ret, "format_name", "") == "IRS fillable forms"
    for name, data in files:
        out.append(("file", _h(data), name))
        try:
            rd = PdfReader(io.BytesIO(data))
        except Exception:
            continue
        ends = _revisions(data)
        whole = data.rstrip()
        # every filled-in version inside the file (earlier saves + the final one)
        first_ids = None
        for i, end in enumerate(ends):
            part = data[:end]
            try:
                prd = rd if part == whole else PdfReader(io.BytesIO(part))
            except Exception:
                continue
            if i == 0:
                first_ids = _ids(prd)
            if fillable and _filled(prd):
                out.append(("filled", _h(part), f"{name} (save {i + 1} of {len(ends)})"))
        ids = _ids(rd)
        if fillable:
            # only IDs the viewer changed after the blank form was downloaded are evidence
            if first_ids is not None and len(ends) > 1:
                for k in ("save_id", "xmp_instance"):
                    if ids.get(k) and ids.get(k) != first_ids.get(k):
                        out.append((k, ids[k], name))
        else:
            for k, v in ids.items():
                out.append((k, v, name))
        info = {}
        try:
            info = {k: str(v) for k, v in (rd.metadata or {}).items()}
        except Exception:
            pass
        created, modified = info.get("/CreationDate", ""), info.get("/ModDate", "")
        software = (info.get("/Producer", "") + " / " + info.get("/Creator", "")).strip(" /")
        if modified and modified != created:
            out.append(("meta", _h(f"{created}|{modified}|{software}"), f"{name}: saved {modified} with {software}"[:200]))
            dt = _pdf_date(modified)
            if dt:
                out.append(("saved_at", dt.strftime("%Y-%m-%dT%H:%M:%S"), name))
        author = info.get("/Author", "").strip()
        if author and not fillable:
            out.append(("author", author[:120], f"{name}: author '{author[:60]}'"))
    if fillable and hasattr(ret, "copies"):
        out += _style(ret)
    # one row per (kind, value)
    seen, uniq = set(), []
    for k, v, d in out:
        if (k, v) not in seen:
            seen.add((k, v))
            uniq.append((k, v, d))
    return uniq


def _style(ret):
    zeros, texts, entries = [], [], []
    for (form, inst), copy in sorted(ret.copies.items()):
        fname = form if inst == 1 else f"{form} #{inst}"
        for tpl, wmap in zip(copy.templates, copy.widgets):
            for w in wmap.values():
                if w.kind == "check":
                    if w.value:
                        entries.append(f"{fname}|{w.short}|{w.state}=X")
                    continue
                raw = w.value or ""
                if raw == "":
                    continue
                entries.append(f"{fname}|{w.short}={raw}")
                if re.fullmatch(r"\$?\s*0+(\.0+)?", raw.strip()):
                    zeros.append(f"{fname}|{tpl.lines.get(w.key) or w.short}")
                elif re.search(r"[A-Za-z]", raw):
                    texts.append(f"{fname}|{w.short}={raw}")
    out = []
    if zeros:
        out.append(("style_zero", _h("\n".join(sorted(zeros))), f"0 typed in {len(zeros)} box(es)"))
    if texts:
        out.append(("style_text", _h("\n".join(sorted(texts))), f"{len(texts)} text entries"))
    if len(entries) >= 5:
        out.append(("all_entries", _h("\n".join(sorted(entries))), f"{len(entries)} filled-in boxes"))
    return out


# ------------------------------------------------------------------ 2. comparing returns
def _blank(v):
    return v is None or str(v).strip() in ("", "0", "0.0", "Unchecked", "Not found", "None")


def analyse(evals, fps, mistakes, roster=None):
    """
    evals:    [{student_id, case_id, name, batch, ...}] - the evaluations in scope
    fps:      [{student_id, case_id, kind, value, detail}] - fingerprints (may include TRAINER rows)
    mistakes: [{student_id, case_id, form, line, student_value, result, needs_review}]
    roster:   [{student_id, name, batch}] (optional, for ID checks)
    Returns a dict of DataFrames: pairs, trainer, groups, students, batches, id_checks.
    """
    ev = {(e["student_id"], e["case_id"]): e for e in evals}
    by_case = defaultdict(set)
    for sid, cid in ev:
        by_case[cid].add(sid)
    pair_pts = defaultdict(lambda: defaultdict(float))      # (case, a, b) -> kind -> points
    pair_txt = defaultdict(dict)                             # (case, a, b) -> kind -> evidence text
    trainer_hits = defaultdict(dict)                         # (case, sid) -> kind -> text

    # ---- fingerprints
    groups = defaultdict(lambda: defaultdict(set))           # (case, kind, value) -> students; details
    details = {}
    trainer_vals = defaultdict(set)                          # case -> {(kind, value)}
    times = defaultdict(list)                                # case -> [(time, sid)]
    for f in fps:
        cid, sid, kind, val = f["case_id"], f["student_id"], f["kind"], f["value"]
        if sid == TRAINER:
            trainer_vals[cid].add((kind, val))
            continue
        if (sid, cid) not in ev:
            continue
        if kind == "saved_at":
            try:
                times[cid].append((datetime.fromisoformat(val), sid))
            except ValueError:
                pass
            continue
        groups[(cid, kind, val)]["s"].add(sid)
        details[(cid, kind, val, sid)] = f.get("detail") or ""
    for (cid, kind, val), g in groups.items():
        studs = sorted(g["s"])
        n = len(by_case[cid])
        if (kind, val) in trainer_vals[cid]:
            if kind in TRAINER_KINDS:
                for s in studs:
                    trainer_hits[(cid, s)][kind] = TRAINER_TEXT[kind]
            continue                                          # template value or trainer leak: not pair evidence
        if len(studs) < 2 or kind not in SIGNALS:
            continue
        common_cut = max(5, n * 0.5) if kind in ("filled", "file") else max(3, n / 3)
        if kind != "file" and len(studs) > common_cut:
            continue                                           # shared by too many: template / normal habit
        pts, text = SIGNALS[kind]
        for i, a in enumerate(studs):
            for b in studs[i + 1:]:
                k = (cid, a, b)
                pair_pts[k][kind] = pts
                pair_txt[k][kind] = text
    # ---- saved close together
    for cid, lst in times.items():
        lst.sort()
        for i, (t, a) in enumerate(lst):
            near = [(u, b) for u, b in lst if b != a and 0 <= (u - t).total_seconds() <= CLOSE_SAVE_SECONDS]
            if len({b for _, b in near}) > 3:                  # a whole lab saving at once is normal
                continue
            for u, b in near:
                x, y = sorted((a, b))
                k = (cid, x, y)
                if "close_save" not in pair_pts[k]:
                    pair_pts[k]["close_save"] = CLOSE_SAVE_POINTS
                    pair_txt[k]["close_save"] = f"Saved within {int(abs((u - t).total_seconds()))} seconds of each other"
    # ---- shared wrong answers (rarity weighted)
    wrong = defaultdict(set)
    shown = {}
    for m in mistakes:
        if (m["student_id"], m["case_id"]) not in ev or m.get("needs_review"):
            continue
        if m["result"] not in ("Wrong", "Unexpected entry") or _blank(m["student_value"]):
            continue
        key = (m["case_id"], str(m["form"]), str(m["line"]), str(m["student_value"]).strip().upper())
        wrong[key].add(m["student_id"])
        shown[key] = f"{m['form']} line {m['line']}: {m['student_value']}"
    shared = defaultdict(list)
    for key, studs in wrong.items():
        cid, n = key[0], len(by_case[key[0]])
        if len(studs) < 2 or len(studs) > max(6, n * 0.2):
            continue                                           # common misconception, not evidence
        w = 1.0 / (len(studs) - 1)
        ss_ = sorted(studs)
        for i, a in enumerate(ss_):
            for b in ss_[i + 1:]:
                shared[(cid, a, b)].append((w, shown[key]))
    for k, items in shared.items():
        wsum = sum(w for w, _ in items)
        pair_pts[k]["wrong"] = min(70.0, round(20 * wsum, 1))
        ex = "; ".join(t for _, t in sorted(items, key=lambda x: -x[0])[:3])
        pair_txt[k]["wrong"] = f"{len(items)} identical wrong entr{'y' if len(items) == 1 else 'ies'} ({ex})"

    # ---- pairs table
    rows = []
    for (cid, a, b), kinds in pair_pts.items():
        txt = pair_txt[(cid, a, b)]
        if "file" in kinds:                                    # identical file: other file signals add nothing
            for k in list(kinds):
                if k in FILE_KINDS and k != "file":
                    kinds.pop(k)
        score = min(100.0, sum(kinds.values()))
        lv = level(score)
        if not lv:
            continue
        ea, eb = ev[(a, cid)], ev[(b, cid)]
        rows.append({"Level": lv, "Score": round(score), "Case": cid,
                     "Student A": a, "Name A": ea.get("name") or "", "Batch A": ea.get("batch") or "",
                     "Student B": b, "Name B": eb.get("name") or "", "Batch B": eb.get("batch") or "",
                     "Evidence": " | ".join(txt[k] for k in sorted(kinds, key=lambda k: -kinds[k]))})
    pairs = pd.DataFrame(rows, columns=["Level", "Score", "Case", "Student A", "Name A", "Batch A", "Student B",
                                        "Name B", "Batch B", "Evidence"])
    if not pairs.empty:
        pairs = pairs.sort_values(["Score", "Case", "Student A"], ascending=[False, True, True]).reset_index(drop=True)

    # ---- trainer file
    trows = []
    for (cid, sid), hits in trainer_hits.items():
        e = ev[(sid, cid)]
        trows.append({"Level": "Very likely copied", "Case": cid, "Student": sid, "Name": e.get("name") or "",
                      "Batch": e.get("batch") or "", "Evidence": " | ".join(hits.values())})
    trainer = pd.DataFrame(trows, columns=["Level", "Case", "Student", "Name", "Batch", "Evidence"])

    # ---- groups (students linked by 'Likely' or stronger, per case)
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for _, r in pairs[pairs["Score"] >= 60].iterrows() if not pairs.empty else []:
        a, b = (r["Case"], r["Student A"]), (r["Case"], r["Student B"])
        parent[find(a)] = find(b)
    clusters = defaultdict(list)
    for x in list(parent):
        clusters[find(x)].append(x)
    grows = []
    for i, members in enumerate(sorted(clusters.values(), key=lambda m: (m[0][0], -len(m))), 1):
        cid = members[0][0]
        sids = sorted(s for _, s in members)
        batches = sorted({ev[(s, cid)].get("batch") or "Unassigned" for s in sids})
        sub = pairs[(pairs["Case"] == cid) & pairs["Student A"].isin(sids) & pairs["Student B"].isin(sids)]
        grows.append({"Group": f"G{i}", "Case": cid, "Students": len(sids), "Student IDs": ", ".join(sids),
                      "Names": ", ".join(ev[(s, cid)].get("name") or "-" for s in sids),
                      "Batches": ", ".join(batches), "Strongest": int(sub["Score"].max()) if not sub.empty else 0})
    groups_df = pd.DataFrame(grows, columns=["Group", "Case", "Students", "Student IDs", "Names", "Batches",
                                             "Strongest"])

    # ---- per student
    flagged = defaultdict(lambda: {"pairs": 0, "best": 0, "cases": set(), "links": set()})
    for _, r in pairs.iterrows() if not pairs.empty else []:
        for me, other in ((r["Student A"], r["Student B"]), (r["Student B"], r["Student A"])):
            f = flagged[me]
            f["pairs"] += 1
            f["best"] = max(f["best"], r["Score"])
            f["cases"].add(r["Case"])
            f["links"].add(other)
    for _, r in trainer.iterrows():
        f = flagged[r["Student"]]
        f["best"], f["trainer"] = 100, True
        f["cases"].add(r["Case"])
    info = {}
    for (sid, cid), e in ev.items():
        x = info.setdefault(sid, {"name": e.get("name") or "", "batch": e.get("batch") or "Unassigned", "n": 0})
        x["n"] += 1
    srows = []
    for sid, x in sorted(info.items(), key=lambda kv: (kv[1]["batch"], kv[0])):
        f = flagged.get(sid)
        srows.append({"Student ID": sid, "Name": x["name"], "Batch": x["batch"], "Returns": x["n"],
                      "Integrity": level(f["best"]) if f else "No concerns",
                      "Highest score": int(f["best"]) if f else 0,
                      "Flagged pairs": f["pairs"] if f else 0,
                      "Built on trainer's file": "Yes" if f and f.get("trainer") else "",
                      "Cases flagged": ", ".join(sorted(f["cases"])) if f else "",
                      "Linked with": ", ".join(sorted(f["links"])) if f else ""})
    students = pd.DataFrame(srows, columns=["Student ID", "Name", "Batch", "Returns", "Integrity", "Highest score",
                                            "Flagged pairs", "Built on trainer's file", "Cases flagged", "Linked with"])

    # ---- per batch
    brows = []
    if not students.empty:
        for b, g in students.groupby("Batch"):
            n = len(g)
            vl = int((g["Integrity"] == "Very likely copied").sum())
            lk = int((g["Integrity"] == "Likely copied").sum())
            rv = int((g["Integrity"] == "Review").sum())
            brows.append({"Batch": b, "Students": n, "Very likely copied": vl, "Likely copied": lk, "Review": rv,
                          "% of students flagged (likely or worse)": round((vl + lk) / n * 100, 1) if n else 0.0})
        tot = students
        n = len(tot)
        vl = int((tot["Integrity"] == "Very likely copied").sum())
        lk = int((tot["Integrity"] == "Likely copied").sum())
        brows.append({"Batch": "ALL BATCHES", "Students": n, "Very likely copied": vl, "Likely copied": lk,
                      "Review": int((tot["Integrity"] == "Review").sum()),
                      "% of students flagged (likely or worse)": round((vl + lk) / n * 100, 1) if n else 0.0})
    batches = pd.DataFrame(brows, columns=["Batch", "Students", "Very likely copied", "Likely copied", "Review",
                                           "% of students flagged (likely or worse)"])
    return {"pairs": pairs, "trainer": trainer, "groups": groups_df, "students": students, "batches": batches,
            "id_checks": id_checks(evals, roster or [])}


def id_checks(evals, roster):
    """Student-ID problems: results for IDs not on the roster, and one name under several IDs."""
    rows = []
    on_roster = {r["student_id"] for r in roster}
    if roster:
        for sid in sorted({e["student_id"] for e in evals} - on_roster):
            cases = sorted({e["case_id"] for e in evals if e["student_id"] == sid})
            rows.append({"Check": "Evaluated ID not on the roster", "Student IDs": sid,
                         "Detail": "Cases: " + ", ".join(cases)})
    names = defaultdict(set)
    for r in roster:
        nm = re.sub(r"[^a-z]", "", str(r.get("name") or "").lower())
        if nm:
            names[nm].add(r["student_id"])
    for r in roster:
        nm = re.sub(r"[^a-z]", "", str(r.get("name") or "").lower())
        if nm and len(names[nm]) > 1:
            ids = ", ".join(sorted(names.pop(nm)))
            rows.append({"Check": "Same name under several IDs", "Student IDs": ids, "Detail": r.get("name") or ""})
    return pd.DataFrame(rows, columns=["Check", "Student IDs", "Detail"])
