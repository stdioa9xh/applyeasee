

import asyncio
import base64
import difflib
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from uagents import Agent, Context, Protocol, Model
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)
try:  # file attachments from ASI:One (chat protocol v0.3+)
    from uagents_core.contrib.protocols.chat import ResourceContent
except ImportError:
    ResourceContent = None

# Multi-Agent Orchestration (A2A)
AGENT_1_ADDRESS = "agent1q2j5twvepd2typxmkv99u8yg83smau40px4ezr2xvel3gwds07xzzn8zawk"  # this agent (ASI:One orchestrator)
AGENT_2_ADDRESS = os.getenv("AGENT_2_ADDRESS",  # Agent 2 · Form Filler; "" disables form filling
                            "agent1q2dtw0yzaf5ssvn590kgue3m8dknn8j2pmw2qzsgn0h900t9neuds3rdwe5")
FILL_TIMEOUT_S = 90  # Agent 2 form preview/submission round trip (Agentverse delivery measured 5–33 s)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  # tested: reads marksheets in ~7 s on the free tier
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",") if m.strip()]
GEMINI_VISION_TIMEOUT_S = 35  # per model attempt; measured 7–17 s for a 2 MB photo
GEMINI_VISION_BUDGET_S = 45   # all attempts together — the user waits for this reply
MAX_IMAGE_SIDE = 1600         # larger photos are shrunk before upload (text stays legible)

# Demo vacancy: a fictional notice + Google Form that exercise the whole pipeline (github.com/stdioa9xh/documentpdf)
DEMO_NOTICE_PDF = "https://raw.githubusercontent.com/stdioa9xh/documentpdf/main/notification.pdf"
DEMO_NOTICE_JSON = "https://raw.githubusercontent.com/stdioa9xh/documentpdf/main/notification.json"
DEMO_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLScWbD6ApjwoqGoubmz9i4qrvZNCnt2nNhasSe8kiQ3Feqko7w/viewform"

# ═══════════════════ Shared A2A schema (IDENTICAL in agentverse_agent.py — edit both) ═══════════════════
class FormFillRequest(Model):
    request_id: str
    candidate_id: str
    form_url: str
    candidate_json: str          # {"full_name": ..., "dob": "DD/MM/YYYY", "category": ..., ...}
    answers_json: str = "{}"     # {"entry.123": "value"} — answers the user typed for specific questions
    submit: bool = False         # False = preview only; True = submit to Google


class FormFillResponse(Model):
    request_id: str
    ok: bool                     # preview: nothing missing/invalid · submit: Google recorded the response
    submitted: bool
    form_title: str
    fields_json: str             # [{"entry_id","title","type","options","required","value","status","reason"}]
    prefill_url: str             # the form pre-filled with every valid value (open, check, submit manually)
    message: str
# ═══════════════════ end of shared schema ═══════════════════


# ═══════════════════ Document audit engine (runs inside Agent 1) ═══════════════════
STORAGE_URL = os.getenv("AGENTVERSE_STORAGE_URL", "https://agentverse.ai/v1/storage")  # SDK default is broken; always pass


FILE_HOSTS = re.compile(r"^https://(res\.cloudinary\.com/fetch-ai/|([a-z0-9-]+\.)?agentverse\.ai/|([a-z0-9-]+\.)?fetch\.ai/)")
MAX_FILE_BYTES = 15 * 1024 * 1024


def storage_download(identity: Any, ref: str) -> Tuple[bytes, str]:
    """Fetches an uploaded file. ASI:One sends uploads as an https link on Fetch.ai's Cloudinary; anything else is
    treated as an Agentverse storage asset id. Only Fetch.ai hosts are fetched, so users can't make the agent
    download arbitrary URLs. Returns (bytes, mime_type)."""
    ref = str(ref)
    if ref.startswith("http"):
        if not FILE_HOSTS.match(ref):
            raise ValueError("file link is not on a Fetch.ai host")
        req = urllib.request.Request(ref, headers={"User-Agent": "ApplyEase-Agent/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            blob = resp.read(MAX_FILE_BYTES + 1)
            if len(blob) > MAX_FILE_BYTES:
                raise ValueError("file is larger than 15 MB")
            return blob, resp.headers.get_content_type()
    from uagents_core.storage import ExternalStorage
    data = ExternalStorage(identity=identity, storage_url=STORAGE_URL).download(ref)
    return base64.b64decode(data["contents"]), data.get("mime_type", "")


DOC_TYPES = {
    "class10": "Class 10 marksheet / certificate",
    "class12": "Class 12 marksheet",
    "diploma": "Diploma certificate",
    "degree": "Graduation degree / marksheet",
    "aadhaar": "Aadhaar card (masked)",
    "obc_ncl": "OBC-NCL certificate (central format)",
    "ews": "EWS Income & Asset certificate",
    "sc_st": "SC / ST caste certificate",
    "pwbd": "Disability (PwBD) certificate",
}


HONORIFICS = {"MR", "MRS", "MS", "MISS", "SHRI", "SRI", "SHREE", "SMT", "KU", "DR"}  # not KUMARI: often part of the name
CERT_TYPES = ("OBC-NCL", "EWS", "SC", "ST", "NONE")
SEVERITY_PENALTY = {"ok": 0, "minor": 15, "major": 35, "critical": 60}


def norm_name(raw: str) -> List[str]:
    s = re.sub(r"[^A-Za-z.\s]", " ", raw or "").upper().replace(".", ". ")
    return [t for t in (w.strip(".") for w in s.split()) if t and t not in HONORIFICS]


def _covers(short: List[str], long: List[str]) -> bool:
    """Every token of `short` appears in `long` in order, as the same word or as its initial."""
    i = 0
    for t in short:
        while i < len(long) and not (long[i] == t or (len(t) == 1 and long[i].startswith(t))
                                     or (len(long[i]) == 1 and t.startswith(long[i]))):
            i += 1
        if i == len(long):
            return False
        i += 1
    return True


def compare_names(gold: str, other: str) -> Dict[str, str]:
    """Classifies how `other` differs from `gold` (the Class 10 name). severity: ok | minor | major | critical."""
    a, b = norm_name(gold), norm_name(other)
    if not a or not b:
        return {"kind": "missing", "severity": "major", "detail": "name not provided"}
    if a == b:
        same_raw = re.sub(r"\s+", " ", gold.strip().upper()) == re.sub(r"\s+", " ", other.strip().upper())
        return {"kind": "exact", "severity": "ok",
                "detail": "names match" if same_raw else "names match (only dots, case or titles differ)"}
    if "".join(a) == "".join(b):
        return {"kind": "spacing", "severity": "minor", "detail": "same letters, different spacing"}
    if sorted(a) == sorted(b):
        return {"kind": "order", "severity": "minor", "detail": "same words in a different order"}
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if _covers(short, long):
        initials = any(len(t) == 1 for t in a + b)
        if len(short) == 1:
            return {"kind": "partial", "severity": "major", "detail": "one document has only a single name"}
        if len(a) == len(b):
            pairs = [f"'{x.title()}' is shortened to '{y}'" if len(y) == 1 else f"'{y.title()}' is shortened to '{x}'"
                     for x, y in zip(a, b) if x != y]
            return {"kind": "initials", "severity": "minor", "detail": "; ".join(pairs)}
        missing = [t for t in long if not any(t == x or (len(x) == 1 and t.startswith(x)) for x in short)]
        return {"kind": "missing_part", "severity": "minor",
                "detail": f"'{' '.join(missing).title()}' appears in only one document" if missing
                else "a name is shortened to its initial"}
    if len(a) == len(b):
        ratios = [difflib.SequenceMatcher(None, x, y).ratio() for x, y in zip(a, b)]
        if min(ratios) >= 0.6:
            diff = [f"{x.title()}/{y.title()}" for x, y in zip(a, b) if x != y]
            sev = "minor" if len(diff) == 1 and min(ratios) >= 0.8 else "major"
            return {"kind": "spelling", "severity": sev, "detail": "spelling differs: " + ", ".join(diff)}
    return {"kind": "different", "severity": "critical", "detail": "the names do not refer to the same spelling of one name"}


def parse_any_date(raw: str) -> Optional[date]:
    s = (raw or "").strip()
    for f in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def fy_start(dt: date) -> date:
    return date(dt.year if dt.month >= 4 else dt.year - 1, 4, 1)


def fy_label(start: date) -> str:
    return f"{start.year}-{str(start.year + 1)[2:]}"


def _dmy(dt: date) -> str:
    return dt.strftime("%d/%m/%Y")


def run_dossier_audit(doc: Dict[str, Any], ref: Optional[date] = None) -> Dict[str, Any]:
    """Deterministic document audit. Uses ONLY what the candidate typed; never invents names, dates or addresses."""
    ref = ref or parse_any_date(doc.get("closing_date", "")) or datetime.now(timezone.utc).date()
    gold = (doc.get("marksheet_name") or "").strip()
    aad = (doc.get("aadhaar_name") or "").strip()
    category = (doc.get("category") or "UR").upper()
    cert = (doc.get("cert_type") or "").upper()
    target = doc.get("target_job") or "government recruitment"
    checks: List[Dict[str, str]] = []
    actions: List[str] = []

    def add(field: str, severity: str, finding: str, remedy: str = "") -> None:
        checks.append({"field": field, "severity": severity, "finding": finding, "remedy": remedy})
        if remedy and remedy not in actions:
            actions.append(remedy)

    # 1. Name: Class 10 is the reference record for central recruitment
    name = compare_names(gold, aad)
    if name["kind"] == "missing":
        add("Name (Class 10 vs Aadhaar)", "major", "Aadhaar name not provided, so the names could not be compared.",
            "Send the name exactly as printed on your Aadhaar card.")
    elif name["severity"] == "ok":
        add("Name (Class 10 vs Aadhaar)", "ok", f"'{gold}' and '{aad}': {name['detail']}.")
    elif name["severity"] == "minor":
        add("Name (Class 10 vs Aadhaar)", "minor", f"'{gold}' vs '{aad}': {name['detail']}.",
            f"Fill the form with your Class 10 name '{gold}' and carry a notarised 'one and the same person' "
            "affidavit to document verification.")
    elif name["severity"] == "major":
        add("Name (Class 10 vs Aadhaar)", "major", f"'{gold}' vs '{aad}': {name['detail']}.",
            f"Update Aadhaar at an Aadhaar Seva Kendra to match '{gold}' (name can be updated twice in a lifetime) "
            "and carry a notarised name affidavit until the update is done.")
    else:
        add("Name (Class 10 vs Aadhaar)", "critical", f"'{gold}' vs '{aad}': {name['detail']}.",
            "An affidavit is not enough for a different name: if the surname changed after marriage, carry the marriage "
            "certificate; otherwise get the change published in the Gazette of India (or the board's correction if the "
            "marksheet is wrong) before applying.")

    # 2. Date of birth: the Class 10 date is final; an affidavit cannot change it
    m_dob, a_dob = parse_any_date(doc.get("marksheet_dob", "")), parse_any_date(doc.get("aadhaar_dob", ""))
    if m_dob and a_dob:
        if m_dob == a_dob:
            add("Date of birth", "ok", f"Both documents show {_dmy(m_dob)}.")
        else:
            add("Date of birth", "critical", f"Class 10 shows {_dmy(m_dob)} but Aadhaar shows {_dmy(a_dob)}.",
                f"Apply with {_dmy(m_dob)} (Class 10 is the only accepted DOB proof) and correct Aadhaar's DOB at an "
                "Aadhaar Seva Kendra (DOB can be updated once).")
    elif doc.get("marksheet_dob") or doc.get("aadhaar_dob"):
        add("Date of birth", "minor", "Only one date of birth was given (or it couldn't be read as DD/MM/YYYY).",
            "Send both dates of birth as DD/MM/YYYY to compare them.")

    # 3. Category certificate validity, judged on the closing date (or today)
    issued = parse_any_date(doc.get("cert_issue_date", ""))
    fy = fy_start(ref)
    if category in ("OBC", "EWS", "SC", "ST") and cert in ("", "NONE"):
        add(f"{category} certificate", "critical", f"No {category} certificate given — without it you are treated as UR "
            "(no age relaxation, no fee waiver, no reserved seat).",
            f"Get a {category} certificate in the central-government format from your Tehsil/SDM office.")
    elif cert in ("OBC-NCL", "EWS"):
        label = "OBC-NCL certificate" if cert == "OBC-NCL" else "EWS Income & Asset certificate"
        if not issued:
            add(label, "major", "Issue date not given, so validity could not be checked.",
                "Send the certificate's issue date (DD/MM/YYYY).")
        elif issued > ref:
            add(label, "major", f"Issue date {_dmy(issued)} is after the reference date {_dmy(ref)}.",
                "Check the issue date printed on the certificate.")
        elif issued >= fy:
            add(label, "ok", f"Issued {_dmy(issued)}, within financial year {fy_label(fy)}.")
        elif cert == "EWS":
            add(label, "critical", f"Issued {_dmy(issued)}, before FY {fy_label(fy)} began — EWS certificates are valid "
                "for one financial year only.",
                f"Apply now for a fresh EWS certificate for FY {fy_label(fy)} at your Tehsil/SDM office.")
        else:
            old = issued < date(fy.year - 1, 4, 1)
            add(label, "critical" if old else "major",
                f"Issued {_dmy(issued)}, before FY {fy_label(fy)} began — most central notifications (e.g. SSC) ask "
                "for a certificate issued in the current financial year. Check the exact window in the notification.",
                f"Apply now for a fresh OBC-NCL certificate (central format, FY {fy_label(fy)}) at your Tehsil/SDM office.")
    elif cert in ("SC", "ST"):
        add(f"{cert} certificate", "ok", "SC/ST certificates have no expiry; make sure it is in the central-government format.")
    if cert in ("OBC-NCL", "EWS", "SC", "ST") and category not in ("", cert.split("-")[0]) and category != "UR":
        add("Category", "major", f"Profile category is {category} but the certificate is {cert}.",
            "Use the category printed on your certificate in the application.")

    if cert == "OBC-NCL" and doc.get("cert_central") == "no":
        add("OBC-NCL format", "major", "The certificate looks like a State-format certificate.",
            "Central posts need the OBC-NCL certificate in the central-government format ('for appointment to posts "
            "under the Government of India') — apply for it at your Tehsil/SDM office.")
    if doc.get("missing_docs"):
        add("Documents not checked", "minor", f"Not uploaded yet: {doc['missing_docs']}.",
            "Upload them here, or keep the originals and 2 self-attested copies ready for document verification.")

    score = max(0, 100 - sum(SEVERITY_PENALTY[c["severity"]] for c in checks))
    worst = max((c["severity"] for c in checks), key=list(SEVERITY_PENALTY).index, default="ok")
    verdict = {"ok": "VERIFIED", "minor": "ACTION_REQUIRED", "major": "ACTION_REQUIRED", "critical": "CRITICAL_RISK"}[worst]
    needs_aff = name["severity"] in ("minor", "major") and name["kind"] != "missing"
    issues = [c for c in checks if c["severity"] != "ok"]
    summary = ("All checked details are consistent — keep the originals and two self-attested copies ready for DV."
               if not issues else
               f"{len(issues)} issue(s) found: " + "; ".join(f"{c['field']} ({c['severity']})" for c in issues)
               + ". Fix them before the closing date — document verification rejects mismatches after the exam.")
    return {
        "verdict": verdict, "compliance_score": score, "summary": summary, "checks": checks, "actions": actions,
        "affidavit_needed": needs_aff,
        "affidavit_text": build_affidavit(doc, target) if needs_aff else "",
    }


def build_affidavit(doc: Dict[str, Any], target: str) -> str:
    gold, aad = doc.get("marksheet_name", "").strip(), doc.get("aadhaar_name", "").strip()
    rel = {"M": "S/o", "F": "D/o"}.get((doc.get("gender") or "").upper(), "S/o / D/o")
    father = (doc.get("father_name") or "").strip() or "[Father's / Mother's name]"
    age = f"{doc['age']}" if doc.get("age") else "[age]"
    dob = parse_any_date(doc.get("marksheet_dob", ""))
    return "\n".join([
        "AFFIDAVIT — NAME DECLARATION (ONE AND THE SAME PERSON)",
        "(Print on non-judicial stamp paper of the value prescribed in your state; sign before a Notary Public "
        "or Executive Magistrate.)",
        "",
        f"I, {gold}, {rel} {father}, aged {age} years, resident of [full address], do hereby solemnly affirm and "
        "declare as under:",
        f"1. That my name is recorded as \"{gold}\" in my Class 10 (Secondary School) certificate"
        + (f", which records my date of birth as {_dmy(dob)}." if dob else "."),
        f"2. That my name is recorded as \"{aad}\" in my Aadhaar card (ending [last 4 digits]).",
        f"3. That \"{gold}\" and \"{aad}\" are the names of one and the same person, namely myself.",
        f"4. That I shall use the name \"{gold}\", as recorded in my Class 10 certificate, in my application for "
        f"{target} and all related records.",
        "",
        f"DEPONENT ({gold})",
        "",
        "VERIFICATION: Verified at [place] on [date] that the contents of paragraphs 1 to 4 above are true and "
        "correct to the best of my knowledge and belief, and nothing material has been concealed.",
        "",
        "DEPONENT",
    ])
# ═══════════════════ end of shared block ═══════════════════


agent = Agent()

IST = timezone(timedelta(hours=5, minutes=30))
EMBEDDED_AS_OF = "21/10/2026"  # curation date of the embedded VACANCIES fallback
DATA_AS_OF = EMBEDDED_AS_OF    # updated by fetch_vacancies() to the live DB's latest sync
CARD_SCHEMA = "applyease.cards/v3"

# ───────────────────────────── Domain constants ─────────────────────────────
# Strict education hierarchy: 10th < 12th < Diploma < Graduate < Post-Graduate
EDU = {1: "10th Pass", 2: "12th Pass (10+2)", 3: "Diploma", 4: "Graduate", 5: "Post-Graduate"}
CAT_RELAX = {"UR": 0, "EWS": 0, "OBC": 3, "SC": 5, "ST": 5}  # upper-age relaxation, central govt norms
PWBD_RELAX = 10  # additive: PwBD-UR 10, PwBD-OBC 13, PwBD-SC/ST 15
CERT_NOTE = {
    "OBC": "OBC-NCL certificate in Central Govt format (creamy layer not eligible)",
    "SC": "SC certificate from competent authority",
    "ST": "ST certificate from competent authority",
    "EWS": "EWS gets reservation but **no age relaxation** in central posts",
}
GENDER = {"M": "Male", "F": "Female"}

# ───────────────────── Vacancy knowledge base (crawler-synced) ─────────────────────
# Dates ISO. fee keys: UR/OBC/EWS/SC/ST/F. relax=False → no category age relaxation.
# physical: height {M,F} cm, st_height {M,F}, chest, pet {M,F}, licence_male, unmarried,
#           gender_only, gender_note, pcm_wings, pwbd_ok, notes[]
VACANCIES: List[Dict[str, Any]] = [
    {
        "id": "ssc-chsl-2026", "short": "SSC CHSL", "rank": 1, "family": "ssc_chsl",
        "title": "SSC CHSL (10+2) — LDC/JSA, PA/SA, DEO",
        "org": "Staff Selection Commission", "posts": 3131, "pay": "Level 2–5 (₹19,900–₹92,300)",
        "opens": "2026-10-14", "last_date": "2026-11-12", "min_age": 18, "max_age": 27, "relax": True,
        "min_edu": 2, "stream": None, "eligibility": "12th pass from a recognised Board (DEO in C&AG: 12th with Maths)",
        "fee": {"UR": "₹100", "OBC": "₹100", "EWS": "₹100", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
        "skill": "Typing 35 WPM (Eng) / 30 WPM (Hindi)", "prep_months": 5,
        "pdf_url": "https://ssc.gov.in/api/attachment/uploads/masterData/NoticeBoards/Notice_of_CHSLE_2026.pdf",
        "apply_url": "https://ssc.gov.in", "aliases": ["chsl", "ldc", "jsa", "deo", "10+2 level"],
    },
    {
        "id": "rrb-ntpc-ug-2026", "short": "RRB NTPC (UG)", "rank": 2, "family": "rrb_ntpc",
        "title": "RRB NTPC Under Graduate — Jr Clerk cum Typist, Accounts Clerk, Trains Clerk, Commercial cum Ticket Clerk",
        "org": "Railway Recruitment Boards", "posts": 3445, "pay": "Level 2–3 (₹19,900–₹21,700 basic)",
        "opens": "2026-10-01", "last_date": "2026-11-20", "min_age": 18, "max_age": 33, "relax": True,
        "min_edu": 2, "stream": None, "eligibility": "12th pass (50% marks for UR/OBC/EWS; not required for SC/ST/PwBD/ExSM)",
        "fee": {"UR": "₹500 (₹400 refunded on CBT-1)", "OBC": "₹500 (₹400 refunded)", "EWS": "₹500 (₹400 refunded)",
                "SC": "₹250 (refunded)", "ST": "₹250 (refunded)", "F": "₹250 (refunded)"},
        "skill": "Typing 30 WPM (Eng) / 25 WPM (Hindi) for typist posts", "prep_months": 5,
        "pdf_url": "https://rrbapply.gov.in/assets/pdf/CEN_07_2026_NTPC_UG.pdf",
        "apply_url": "https://rrbapply.gov.in", "aliases": ["ntpc", "railway clerk", "ticket clerk", "rrb ug"],
    },
    {
        "id": "ssc-gd-2027", "short": "SSC GD Constable", "rank": 3, "family": "ssc_gd",
        "title": "SSC GD Constable 2027 — BSF, CISF, CRPF, SSB, ITBP, AR, SSF, NCB",
        "org": "Staff Selection Commission", "posts": 25487, "pay": "Level 3 (₹21,700–₹69,100)",
        "opens": "2026-10-10", "last_date": "2026-11-30", "min_age": 18, "max_age": 23, "relax": True,
        "min_edu": 1, "stream": None, "eligibility": "10th pass + PET/PST + medical",
        "fee": {"UR": "₹100", "OBC": "₹100", "EWS": "₹100", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
        "skill": "Running: 5 km / 1.6 km (see PET)", "prep_months": 4,
        "physical": {
            "height": {"M": 170, "F": 157}, "st_height": {"M": 162.5, "F": 150},
            "chest": "Male: 80 cm + 5 cm expansion",
            "pet": {"M": "5 km in 24 min", "F": "1.6 km in 8.5 min"}, "pwbd_ok": False,
            "notes": ["Height relaxation for hill areas, NE states, Gorkha/Garhwali/Kumaoni & LWE districts — check notification"],
        },
        "pdf_url": "https://ssc.gov.in/api/attachment/uploads/masterData/NoticeBoards/Notice_GD_2027.pdf",
        "apply_url": "https://ssc.gov.in", "aliases": ["gd", "constable", "capf constable", "bsf", "crpf"],
    },
    {
        "id": "rrb-group-d-2026", "short": "RRB Group D", "rank": 4, "family": "rrb_ntpc",
        "title": "RRB Group D (Level-1) — Track Maintainer, Assistant, Pointsman",
        "org": "Railway Recruitment Boards", "posts": 32438, "pay": "Level 1 (₹18,000 basic)",
        "opens": "2026-12-01", "last_date": "2026-12-31", "min_age": 18, "max_age": 36, "relax": True,
        "min_edu": 1, "stream": None, "eligibility": "10th pass or ITI / NAC from NCVT",
        "fee": {"UR": "₹500 (₹400 refunded)", "OBC": "₹500 (₹400 refunded)", "EWS": "₹500 (₹400 refunded)",
                "SC": "₹250 (refunded)", "ST": "₹250 (refunded)", "F": "₹250 (refunded)"},
        "skill": "PET (weight carry + 1000 m run)", "prep_months": 4,
        "physical": {
            "pet": {"M": "35 kg carry 100 m in 2 min + 1000 m in 4 min 15 s",
                    "F": "20 kg carry 100 m in 2 min + 1000 m in 5 min 40 s"},
            "pwbd_ok": True, "notes": ["PwBD candidates are exempt from PET; only identified posts open to PwBD"],
        },
        "pdf_url": "https://rrbapply.gov.in/assets/pdf/CEN_08_2026_Level1.pdf",
        "apply_url": "https://rrbapply.gov.in", "aliases": ["group d", "level 1", "track maintainer"],
    },
    {
        "id": "upsc-nda-i-2027", "short": "UPSC NDA & NA (I)", "rank": 5, "family": "nda",
        "title": "UPSC NDA & NA (I) 2027 — Army, Navy, Air Force Officer Entry",
        "org": "Union Public Service Commission", "posts": 406, "pay": "Lieutenant: Level 10 (₹56,100) after training",
        "opens": "2026-12-17", "last_date": "2027-01-06", "min_age": 16, "max_age": 19, "relax": False,
        "min_edu": 2, "stream": None, "eligibility": "12th pass/appearing; Air Force & Navy wings need Physics + Maths",
        "fee": {"UR": "₹100", "OBC": "₹100", "EWS": "₹100", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
        "skill": "SSB interview + medical", "prep_months": 5,
        "physical": {
            "height": {"M": 157, "F": 152}, "unmarried": True, "pcm_wings": True, "pwbd_ok": False,
            "gender_note": "Open to men & women (all three wings)",
            "notes": ["Air Force flying branch: min height 162.5 cm", "Medical & vision standards per MoD at SSB stage"],
        },
        "pdf_url": "https://upsc.gov.in/sites/default/files/Notif-NDA-NA-I-2027-engl.pdf",
        "apply_url": "https://upsconline.nic.in", "aliases": ["nda", "naval academy", "defence officer after 12th"],
    },
    {
        "id": "ssc-mts-2026", "short": "SSC MTS & Havaldar", "rank": 6, "family": "ssc_chsl",
        "title": "SSC MTS (Non-Technical) & Havaldar (CBIC/CBN)",
        "org": "Staff Selection Commission", "posts": 8021, "pay": "Level 1 (₹18,000 basic)",
        "opens": "2026-06-26", "last_date": "2026-07-24", "min_age": 18, "max_age": 25, "relax": True,
        "min_edu": 1, "stream": None, "eligibility": "10th pass (Havaldar additionally: PET/PST)",
        "fee": {"UR": "₹100", "OBC": "₹100", "EWS": "₹100", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
        "skill": "—", "prep_months": 4, "next_cycle": "Jun–Jul 2027",
        "physical": {
            "height": {"M": 157.5, "F": 152}, "pet": {"M": "Walk 1600 m in 15 min", "F": "Walk 1 km in 20 min"},
            "pwbd_ok": True, "notes": ["Height/walk standards apply to **Havaldar only**; MTS has none", "Havaldar not open to PwBD"],
        },
        "pdf_url": "https://ssc.gov.in/api/attachment/uploads/masterData/NoticeBoards/Notice_MTS_2026.pdf",
        "apply_url": "https://ssc.gov.in", "aliases": ["mts", "havaldar", "multi tasking"],
    },
    {
        "id": "rrb-je-2026", "short": "RRB JE", "rank": 10, "family": "technical",
        "title": "RRB Junior Engineer, DMS & CMA",
        "org": "Railway Recruitment Boards", "posts": 7951, "pay": "Level 6 (₹35,400–₹1,12,400)",
        "opens": "2026-10-06", "last_date": "2026-11-05", "min_age": 18, "max_age": 33, "relax": True,
        "min_edu": 3, "stream": "engineering", "eligibility": "3-yr Diploma or B.E./B.Tech in Civil/Mech/Electrical/Electronics/allied",
        "fee": {"UR": "₹500 (₹400 refunded)", "OBC": "₹500 (₹400 refunded)", "EWS": "₹500 (₹400 refunded)",
                "SC": "₹250 (refunded)", "ST": "₹250 (refunded)", "F": "₹250 (refunded)"},
        "skill": "—", "prep_months": 6,
        "pdf_url": "https://rrbapply.gov.in/docs/CEN_03_2026_JE_Detailed.pdf",
        "apply_url": "https://rrbapply.gov.in", "aliases": ["je", "junior engineer", "rrb je"],
    },
    {
        "id": "mpesb-sub-eng-2026", "short": "MPESB Sub Engineer", "rank": 11, "family": "technical",
        "title": "MPESB Group-3 Sub Engineer & Technician",
        "org": "MP Employee Selection Board (State)", "posts": 1700, "pay": "MP Pay Matrix Level 9",
        "opens": "2026-10-13", "last_date": "2026-10-27", "min_age": 18, "max_age": 40, "relax": True,
        "min_edu": 3, "stream": "engineering", "eligibility": "Diploma / B.E. / B.Tech (Civil/Electrical/Mechanical) or ITI for technician",
        "fee": {"UR": "₹560", "OBC": "₹310 (MP domicile)", "EWS": "₹310 (MP domicile)", "SC": "₹310", "ST": "₹310", "F": "₹560"},
        "skill": "—", "prep_months": 4,
        "pdf_url": "https://esb.mp.gov.in/Rulebooks/RB_2026/Group03_2026_Updated_27082026.pdf",
        "apply_url": "https://esb.mponline.gov.in/", "aliases": ["mpesb", "sub engineer", "mp vyapam"],
    },
    {
        "id": "ssc-cpo-si-2026", "short": "SSC CPO SI", "rank": 12, "family": "ssc_grad",
        "title": "SSC CPO Sub-Inspector — Delhi Police & CAPFs",
        "org": "Staff Selection Commission", "posts": 4187, "pay": "Level 6 (₹35,400–₹1,12,400)",
        "opens": "2026-10-16", "last_date": "2026-11-15", "min_age": 20, "max_age": 25, "relax": True,
        "min_edu": 4, "stream": None, "eligibility": "Bachelor's degree in any discipline",
        "fee": {"UR": "₹100", "OBC": "₹100", "EWS": "₹100", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
        "skill": "PET: sprint + 1.6 km / 800 m + long jump", "prep_months": 6,
        "physical": {
            "height": {"M": 170, "F": 157}, "st_height": {"M": 162.5, "F": 154},
            "chest": "Male: 80 cm + 5 cm expansion",
            "pet": {"M": "100 m in 16 s · 1.6 km in 6.5 min · long jump 3.65 m · shot put 4.5 m",
                    "F": "100 m in 18 s · 800 m in 4 min · long jump 2.7 m"},
            "licence_male": True, "pwbd_ok": False,
            "notes": ["Hill-area & NE-state height relaxations apply — check notification"],
        },
        "pdf_url": "https://ssc.gov.in/api/pdf/cpo_si_2026_official_gazette.pdf",
        "apply_url": "https://ssc.gov.in", "aliases": ["cpo", "sub inspector", "si", "delhi police"],
    },
    {
        "id": "ssc-cgl-2027", "short": "SSC CGL", "rank": 13, "family": "ssc_grad",
        "title": "SSC CGL 2027 — ASO, Inspector (Income Tax/GST), Auditor, JSO",
        "org": "Staff Selection Commission", "posts": 14582, "pay": "Level 4–7 (up to ₹1,42,400)",
        "opens": "2027-04-01", "last_date": "2027-05-01", "min_age": 18, "max_age": 32, "relax": True,
        "min_edu": 4, "stream": None, "eligibility": "Bachelor's degree in any discipline (JSO: Maths/Stats)",
        "fee": {"UR": "₹100", "OBC": "₹100", "EWS": "₹100", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
        "skill": "DEST typing for CSS/ASO", "prep_months": 6,
        "physical": {"notes": ["Inspector (CBN/Excise) & SI (CBI/NIA) posts carry physical standards — check post-wise annexure"]},
        "pdf_url": "https://ssc.gov.in", "apply_url": "https://ssc.gov.in", "aliases": ["cgl", "income tax inspector", "aso"],
    },
    {
        "id": "ibps-po-xiv", "short": "IBPS PO", "rank": 14, "family": "bank",
        "title": "IBPS Probationary Officer / MT (CRP PO-XIV)",
        "org": "Institute of Banking Personnel Selection", "posts": 4455, "pay": "JMGS-I (₹48,480 basic)",
        "opens": "2026-10-20", "last_date": "2026-11-10", "min_age": 20, "max_age": 30, "relax": True,
        "min_edu": 4, "stream": None, "eligibility": "Graduation in any discipline",
        "fee": {"UR": "₹850", "OBC": "₹850", "EWS": "₹850", "SC": "₹175", "ST": "₹175", "F": "₹850"},
        "skill": "—", "prep_months": 5,
        "pdf_url": "https://ibps.in/notifications/CRP_PO_XIV_Detailed.pdf",
        "apply_url": "https://ibps.in", "aliases": ["ibps", "bank po", "banking"],
    },
    {
        "id": "upsc-cds-i-2027", "short": "UPSC CDS (I)", "rank": 15, "family": "nda",
        "title": "UPSC Combined Defence Services (I) 2027 — IMA, INA, AFA, OTA",
        "org": "Union Public Service Commission", "posts": 457, "pay": "Lieutenant: Level 10 (₹56,100)",
        "opens": "2026-12-10", "last_date": "2026-12-30", "min_age": 19, "max_age": 24, "relax": False,
        "min_edu": 4, "stream": None, "eligibility": "Degree for IMA/OTA; Degree with Physics & Maths or B.Tech for AFA/INA",
        "fee": {"UR": "₹200", "OBC": "₹200", "EWS": "₹200", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
        "skill": "SSB interview", "prep_months": 6,
        "physical": {
            "height": {"M": 157.5, "F": 152}, "unmarried": True, "pcm_wings": True, "pwbd_ok": False,
            "gender_note": "IMA & INA: men only · OTA: men & women · AFA: verify gender eligibility in notification",
            "notes": ["Air Force flying branch: min height 162.5 cm"],
        },
        "pdf_url": "https://upsc.gov.in/sites/default/files/Notif-CDSE-I-2027-engl.pdf",
        "apply_url": "https://upsconline.nic.in", "aliases": ["cds", "ima", "ota", "army officer"],
    },
    {
        "id": "upsc-cse-2027", "short": "UPSC Civil Services", "rank": 16, "family": "upsc_cse",
        "title": "UPSC Civil Services Exam 2027 — IAS, IPS, IFS, IRS",
        "org": "Union Public Service Commission", "posts": 979, "pay": "Level 10 (₹56,100) onwards",
        "opens": "2027-01-22", "last_date": "2027-02-11", "min_age": 21, "max_age": 32, "relax": True,
        "min_edu": 4, "stream": None, "eligibility": "Any degree. Attempts: UR/EWS 6, OBC 9, SC/ST unlimited (till age limit)",
        "fee": {"UR": "₹100", "OBC": "₹100", "EWS": "₹100", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
        "skill": "Prelims + Mains + Interview", "prep_months": 12,
        "physical": {"notes": ["IPS & some Group-A services have physical/medical standards"]},
        "pdf_url": "https://upsc.gov.in", "apply_url": "https://upsconline.nic.in",
        "aliases": ["civil services", "upsc cse", "ias", "ips", "cse"],
    },
]

# ── Demo vacancies: a fictional board whose notices (github.com/stdioa9xh/documentpdf/notices) and one Google Form
#    exercise the whole pipeline. One per qualification level and age band, so any tester has an eligible one.
DEMO_BASE = "https://raw.githubusercontent.com/stdioa9xh/documentpdf/main/notices"
GD_PHYSICAL = {
    "height": {"M": 170, "F": 157}, "st_height": {"M": 162.5, "F": 150}, "chest": "Male: 80 cm + 5 cm expansion",
    "pet": {"M": "5 km in 24 min", "F": "1.6 km in 8.5 min"}, "pwbd_ok": False,
    "notes": ["Height relaxation for ST candidates and hill / North-East areas — see the notice"],
}


def _demo(vid: str, short: str, title: str, posts: int, pay: str, closes: str, lo: int, hi: int, edu: int,
          elig: str, family: str, prep: int, skill: str, aliases: List[str], **extra: Any) -> Dict[str, Any]:
    return {"id": vid, "short": short, "rank": 0, "family": family, "title": title,
            "org": "ApplyEase Demo Recruitment Board (fictional)", "posts": posts, "pay": pay,
            "opens": "2026-10-24", "last_date": closes, "min_age": lo, "max_age": hi, "relax": True,
            "min_edu": edu, "stream": None, "eligibility": f"{elig} (age as on 01/01/2027)",
            "fee": {"UR": "₹100", "OBC": "₹100", "EWS": "₹100", "SC": "Exempt", "ST": "Exempt", "F": "Exempt"},
            "skill": skill, "prep_months": prep, "pdf_url": f"{DEMO_BASE}/{vid}.pdf",
            "notice_json": f"{DEMO_BASE}/{vid}.json", "apply_url": DEMO_FORM_URL, "form_url": DEMO_FORM_URL,
            "demo": True, "aliases": aliases, **extra}


VACANCIES += [
    _demo("adrb-mts-demo-2026", "ADRB MTS (Demo)", "Multi-Tasking Staff (Demo) 2026", 2400, "Level 1 (₹18,000–₹56,900)",
          "2026-11-20", 18, 25, 1, "10th pass", "ssc_chsl", 4, "—", ["adrb mts", "demo mts"]),
    _demo("adrb-constable-demo-2026", "ADRB Constable (Demo)", "Constable (General Duty) (Demo) 2026", 3000,
          "Level 3 (₹21,700–₹69,100)", "2026-11-30", 18, 23, 1, "10th pass + PST/PET + medical", "ssc_gd", 5,
          "Running: 5 km in 24 min (men) / 1.6 km in 8.5 min (women)", ["adrb constable", "demo constable"],
          physical=GD_PHYSICAL),
    _demo("adrb-ldc-demo-2026", "ADRB LDC (Demo)", "Lower Division Clerk & Data Entry Operator (Demo) 2026", 1800,
          "Level 2–4 (₹19,900–₹81,100)", "2026-11-22", 18, 27, 2, "12th pass", "ssc_chsl", 5,
          "Typing 35 WPM (Eng) / 30 WPM (Hindi)", ["adrb ldc", "demo ldc", "demo clerk"]),
    _demo("adrb-tech-demo-2026", "ADRB Jr Technician (Demo)", "Junior Technician — Civil/Mech/Elec (Demo) 2026", 900,
          "Level 6 (₹35,400–₹1,12,400)", "2026-12-05", 18, 30, 3, "3-year Engineering Diploma (B.E./B.Tech also eligible)",
          "technical", 6, "Engineering discipline paper practice", ["adrb technician", "junior technician", "demo technician"],
          stream="engineering"),
    _demo("adrb-cgle-demo-2026", "ADRB CGLE (Demo)", "Combined Graduate Level Exam (Demo) 2026 — Junior Assistant, "
          "Data Entry Supervisor", 1700, "Level 4–5 (₹25,500–₹92,300)", "2026-11-25", 18, 27, 4,
          "Bachelor's degree in any discipline", "ssc_grad", 6, "Data Entry Speed Test for Data Entry Supervisor",
          ["adrb cgle", "demo cgle", "cgle", "junior assistant", "data entry supervisor"]),
    _demo("adrb-aso-demo-2026", "ADRB ASO (Demo)", "Assistant Section Officer (Demo) 2026", 600,
          "Level 7 (₹44,900–₹1,42,400)", "2026-11-28", 20, 30, 4, "Bachelor's degree in any discipline", "ssc_grad", 8,
          "Computer Proficiency Test", ["adrb aso", "demo aso"]),
    _demo("adrb-senior-asst-demo-2026", "ADRB Senior Asst (Demo)", "Senior Assistant — Lateral Entry (Demo) 2026", 300,
          "Level 6 (₹35,400–₹1,12,400)", "2026-12-10", 25, 40, 4, "Bachelor's degree in any discipline", "ssc_grad", 6,
          "Interview preparation", ["adrb senior assistant", "senior assistant", "demo senior assistant"]),
    _demo("adrb-research-demo-2026", "ADRB Research Officer (Demo)", "Research Officer (Demo) 2026", 150,
          "Level 10 (₹56,100–₹1,77,500)", "2026-12-15", 21, 35, 5,
          "Master's in Economics / Statistics / Maths / Commerce / Public Policy", "ssc_grad", 8,
          "Subject paper + interview", ["adrb research officer", "research officer", "demo research officer"]),
]
MAX_DEMOS_SHOWN = 3


def featured_first(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Up to MAX_DEMOS_SHOWN demo posts first (highest qualification the user meets first), then the real posts."""
    demos = sorted((j for j in jobs if j.get("demo")), key=lambda j: (-j["min_edu"], j["last_date"]))
    return demos[:MAX_DEMOS_SHOWN] + [j for j in jobs if not j.get("demo")]


JOB_BY_ID = {j["id"]: j for j in VACANCIES}

# ───────────────────── Live retrieval: Supabase `vacancies` (crawler-synced) ─────────────────────
# Read-only PostgREST GET with the anon key. Set as Agentverse agent secrets / env vars:
#   SUPABASE_URL, SUPABASE_ANON_KEY   (never the service-role key)
# Columns are the ones sarkari_crawler.sync_to_supabase() upserts. The crawler writes no `status`
# or `eligibility` column, so recruitment-only filtering and min_edu inference happen here.
LIVE_TTL_S = 15 * 60
FAIL_TTL_S = 2 * 60  # after a failure, retry soon but don't hit the DB on every message
LIVE_TIMEOUT_S = 4
LIVE_COLUMNS = ("id,title,org,post_name,total_posts,detail_url,dates,fees,age_limit,links,"
                "notification_pdf_storage_url,source_tag,updated_at")
_CACHE: Dict[str, Any] = {"jobs": VACANCIES, "source": "cached", "live_count": 0, "ts": 0.0, "ttl": 0}

NOT_RECRUITMENT = re.compile(r"\b(admit card|answer key|result|cut ?off|syllabus|cancel+ed|exam city|score ?card|merit list)\b", re.I)
UNMODELLED = re.compile(r"\b(teacher|tgt|pgt|prt|tet|b\.?ed|professor|lecturer|judge|judicial|law|medical officer|"
                        r"doctor|mbbs|nurs\w*|pharmac\w*|scientist|apprentice\w*|scholarship|admission)\b", re.I)
EDU_HINTS = [  # (level, pattern) — highest match wins, so mixed titles get the stricter bar
    (4, r"\b(graduat\w*|degree|cgl|po|probationary|officer|inspector|sub inspector|bank|ibps|sbi|clerk|cds|afcat|cse|ias)\b"),
    (3, r"\b(diploma|polytechnic|junior engineer|je|sub engineer)\b"),
    (2, r"\b(12th|10\s?\+\s?2|intermediate|inter pass|chsl|ntpc under ?graduate|ntpc ug|nda)\b"),
    (1, r"\b(10th|matric\w*|group d|level[- ]?1|mts|constable|gd|havaldar|agniveer)\b"),
]
NO_RELAX = re.compile(r"\b(nda|cds|afcat|agniveer|agnipath|army|navy|air force|coast guard)\b", re.I)
UNIFORMED = re.compile(r"\b(police|constable|gd|si|sub inspector|capf|bsf|crpf|cisf|itbp|ssb|agniveer|army|navy|"
                       r"air force|nda|cds|havaldar|fireman|forest guard|jail warder)\b", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _secret(name: str) -> str:
    return re.sub(r"\s+", "", os.environ.get(name, ""))  # secrets pasted with stray newlines broke the crawler before


def parse_date(raw: Any) -> Optional[date]:
    """'27/10/2026', '27-10-2026', '27 October 2026', 'October 27, 2026', ISO → date; anything else → None."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b", s)
        if m:
            dd, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\.?,?\s+(\d{4})\b", s) or \
                re.search(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", s)
            if not m:
                return None
            a, b, y = m.group(1), m.group(2), int(m.group(3))
            dd, mon = (int(a), b) if a.isdigit() else (int(b), a)
            if mon not in MONTHS:
                return None
            mo = MONTHS[mon]
    try:
        return date(y, mo, dd)
    except ValueError:
        return None


def _int(raw: Any) -> Optional[int]:
    m = re.search(r"\d+", str(raw or "").replace(",", ""))
    return int(m.group(0)) if m else None


def _fee(raw: Any) -> Optional[str]:
    n = _int(raw)
    if n is None:
        return None
    return "Exempt" if n == 0 else f"₹{n}"


def _url(*cands: Any) -> Optional[str]:
    return next((c.strip() for c in cands if isinstance(c, str) and c.strip().startswith("http")), None)


def match_embedded(title: str) -> Optional[Dict[str, Any]]:
    """Embedded entry for a live row: same recruiter (first word of `short`) + longest alias (≥3 chars) in title."""
    t = " " + title.lower() + " "
    best, best_len = None, 0
    for job in VACANCIES:
        recruiter = job["short"].split()[0].lower()
        if not (re.search(r"\b" + re.escape(recruiter) + r"\b", t) or (recruiter == "rrb" and "railway" in t)):
            continue
        for a in job["aliases"] + [job["short"].lower()]:
            if len(a) >= 3 and len(a) > best_len and re.search(r"\b" + re.escape(a) + r"\b", t):
                best, best_len = job, len(a)
    return best


def infer_family(text: str, edu: int, engg: bool) -> str:
    t = text.lower()
    if engg:
        return "technical"
    if re.search(r"\b(bank|ibps|sbi|rbi|nabard)\b", t):
        return "bank"
    if re.search(r"\b(nda|cds|afcat|navy|air force|army)\b", t):
        return "nda"
    if re.search(r"\b(upsc|ias|cse|psc)\b", t) and edu >= 4:
        return "upsc_cse"
    if re.search(r"\b(railway|rrb|rrc)\b", t):
        return "rrb_ntpc"
    if UNIFORMED.search(t):
        return "ssc_gd" if edu <= 2 else "ssc_grad"
    return "ssc_chsl" if edu <= 2 else "ssc_grad"


def short_name(title: str) -> str:
    s = re.sub(r"\b(online form|recruitment|notification|apply online|vacancy|bharti|re ?open\w*|date extended|"
               r"for \d+ posts?|\d+ posts?|20\d\d)\b", " ", title, flags=re.I)
    return " ".join(s.replace("|", " ").split()[:4]) or title[:30]


def row_to_job(row: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    """Maps a crawler row to the agent's job dict. Returns None when the row can't be assessed safely."""
    title = (row.get("title") or row.get("post_name") or "").strip()
    if not title or NOT_RECRUITMENT.search(title):
        return None
    dates, fees = row.get("dates") or {}, row.get("fees") or {}
    age, links = row.get("age_limit") or {}, row.get("links") or {}
    # Listing date wins when later: re-opened / extended forms keep the stale detail-page date.
    lasts = [x for x in (parse_date(dates.get("listing_last_date")), parse_date(dates.get("last_date_apply"))) if x]
    if not lasts:
        return None
    last = max(lasts)
    if last < today() - timedelta(days=45):
        return None
    synced = parse_date((row.get("updated_at") or "")[:10]) or today()
    opens = parse_date(dates.get("application_begin")) or min(synced, last)
    if opens > last:
        opens = min(synced, last)
    posts = _int(row.get("total_posts")) or 0
    pdf = _url(row.get("notification_pdf_storage_url"), links.get("notification_pdf"), row.get("detail_url"))
    apply = _url(links.get("apply_online"), links.get("official_website"), row.get("detail_url"))
    live = {"_src": "live", "detail_url": row.get("detail_url"), "opens": opens.isoformat(), "last_date": last.isoformat()}
    if posts:
        live["posts"] = posts
    if pdf:
        live["pdf_url"] = pdf
    if apply:
        live["apply_url"] = apply

    base = match_embedded(title)
    if base:  # curated rules (edu, stream, ages, relaxation, fees, physical) + live numbers, dates & links
        return {**base, **live}

    # Unknown post: infer conservatively, or skip rather than guess eligibility.
    blob = f"{title} {row.get('post_name') or ''} {row.get('org') or ''}"
    if UNMODELLED.search(blob):
        return None
    edu = max((lvl for lvl, pat in EDU_HINTS if re.search(pat, blob, re.I)), default=0)
    max_age = _int(age.get("max_age"))
    if not edu or not max_age or not apply:
        return None
    engg = edu == 3 or bool(re.search(r"\b(engineer\w*|technical|technician)\b", blob, re.I))
    if engg:
        edu = max(edu, 3)
    min_age = _int(age.get("min_age")) or 18
    fam = infer_family(blob, edu, engg)
    ur, sc = _fee(fees.get("general_obc")), _fee(fees.get("sc_st"))
    ur_txt = ur or "See notification"
    short = short_name(title)
    job = {
        "id": str(row.get("id") or f"live-{idx}"), "short": short, "rank": 100 + idx, "family": fam,
        "title": title, "org": row.get("org") or "See notification", "posts": posts,
        "pay": "As per notification", "min_age": min_age, "max_age": max(max_age, min_age),
        "relax": not NO_RELAX.search(blob), "min_edu": edu, "stream": "engineering" if engg else None,
        "eligibility": f"{EDU[edu]}{' (Engineering)' if engg else ''} or higher (inferred from title — verify post-wise "
                       "eligibility in the notification)",
        "fee": {"UR": ur_txt, "OBC": ur_txt, "EWS": ur_txt, "SC": sc or ur_txt, "ST": sc or ur_txt,
                "F": f"{ur_txt} (women's concession: check notification)"},
        "skill": "—", "prep_months": sum(m for m, _ in ROADMAPS[fam]), "aliases": [short.lower()],
        **live,
    }
    if UNIFORMED.search(blob):
        job["physical"] = {"pwbd_ok": False, "notes": ["Physical standards (PST/PET) & medical apply — see notification"]}
    return job


def _get_rows(url: str, key: str) -> List[Dict[str, Any]]:
    qs = urllib.parse.urlencode({"select": LIVE_COLUMNS, "order": "updated_at.desc", "limit": "200"}, safe=",.")
    req = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/vacancies?{qs}", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=LIVE_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"unexpected PostgREST payload: {str(data)[:120]}")
    return data


def fetch_vacancies(force: bool = False) -> List[Dict[str, Any]]:
    """Live jobs from Supabase (cached ~15 min), merged over the embedded list; embedded list on any failure."""
    global DATA_AS_OF
    now = time.monotonic()
    if not force and _CACHE["ts"] and now - _CACHE["ts"] < _CACHE["ttl"]:
        return _CACHE["jobs"]
    url, key = _secret("SUPABASE_URL"), _secret("SUPABASE_ANON_KEY")
    jobs, source, live_count, as_of, err = VACANCIES, "cached", 0, EMBEDDED_AS_OF, None
    if url and key:
        try:
            rows = _get_rows(url, key)
            mapped: Dict[str, Dict[str, Any]] = {}
            for i, row in enumerate(rows):  # newest first → first row per job id wins
                job = row_to_job(row, i)
                if job and job["id"] not in mapped:
                    mapped[job["id"]] = job
            if mapped:
                jobs = list(mapped.values()) + [j for j in VACANCIES if j["id"] not in mapped]
                source, live_count = "live", len(mapped)
                synced = max((r.get("updated_at") or "" for r in rows), default="")
                try:
                    as_of = datetime.fromisoformat(synced.replace("Z", "+00:00")).astimezone(IST).strftime("%d/%m/%Y %H:%M IST")
                except ValueError:
                    as_of = fmt(today())
            else:
                err = f"{len(rows)} rows, none usable"
        except Exception as e:  # network, auth/RLS, schema — never break the chat
            err = f"{type(e).__name__}: {e}"
    else:
        err = "SUPABASE_URL / SUPABASE_ANON_KEY not set"
    DATA_AS_OF = as_of
    _CACHE.update(jobs=jobs, source=source, live_count=live_count, ts=now, error=err,
                  ttl=LIVE_TTL_S if source == "live" else FAIL_TTL_S)
    return jobs


def data_footer() -> str:
    if _CACHE["source"] == "live":
        curated = len(_CACHE["jobs"]) - _CACHE["live_count"]
        return (f"_🟢 **Live** data: {_CACHE['live_count']} posts from the ApplyEase crawler DB (synced {DATA_AS_OF})"
                + (f" + {curated} curated" if curated else "") + ". Always verify dates, relaxations & physical "
                "standards in the official notification._")
    return (f"_🟡 **Cached** data as of {DATA_AS_OF} (live DB unavailable — offline knowledge base). Always verify dates, "
            "relaxations & physical standards in the official notification._")


def posts_txt(j: Dict[str, Any]) -> str:
    return f"{j['posts']:,}" if j.get("posts") else "see notification"

# ─────────────────────────── Preparation roadmaps (Tier 3) ───────────────────────────
ROADMAPS: Dict[str, List[Tuple[int, str]]] = {  # (months, focus)
    "ssc_chsl": [
        (2, "**Foundations** — Quant (%, ratio, SI/CI, TSD, mensuration), Reasoning (series, coding, syllogism), English grammar + 20 words/day"),
        (2, "**PYQs & GA** — last 5 years' PYQs topic-wise, daily current affairs (30 min) + static GK, sectional tests"),
        (1, "**Mocks & Skill** — 1 full mock/day + error log, typing practice to **35 WPM** for Tier-2 skill test"),
    ],
    "ssc_gd": [
        (2, "**Foundations** — Elementary Maths, Reasoning, basic English/Hindi + start running 3 km daily"),
        (1, "**PYQs & GK** — PYQs, static GK + current affairs; build to target PET distance/time"),
        (1, "**Mocks & PET** — daily CBT mocks, timed PET runs, height/chest documents ready"),
    ],
    "rrb_ntpc": [
        (2, "**Foundations** — Maths (arithmetic, algebra), Reasoning (puzzles, analogies)"),
        (1, "**General Awareness** — NCERT Science 6–10, Indian Railways facts, current affairs"),
        (1, "**PYQs & CBT-1** — RRB PYQs (all shifts), sectional + full CBT-1 mocks"),
        (1, "**CBT-2 & Skill** — CBT-2 mocks, typing / PET practice, document verification prep"),
    ],
    "nda": [
        (2, "**Maths (300 marks)** — algebra, trigonometry, calculus, vectors, statistics"),
        (1, "**GAT** — English + Physics/Chemistry NCERT, History/Geography basics"),
        (1, "**PYQs & Current Affairs** — 10 years' PYQs, defence & world affairs"),
        (1, "**Mocks & SSB** — full mocks, OIR/PPDT practice, 2.4 km run, push-ups/sit-ups"),
    ],
    "technical": [
        (3, "**Core Branch** — Civil/Mech/Electrical fundamentals, formula notebook"),
        (1, "**General Sections** — Maths, Reasoning, General Science, GA"),
        (2, "**PYQs & Mocks** — RRB JE/SSC JE PYQs, CBT-1 + CBT-2 full mocks"),
    ],
    "ssc_grad": [
        (2, "**Foundations** — advanced Quant (algebra, geometry, trigonometry), Reasoning, English (RC, cloze)"),
        (2, "**PYQs & GA** — PYQs, static GK + 6 months' current affairs"),
        (2, "**Mocks & Physical/Skill** — daily mocks; PET running (CPO) / DEST typing (CGL)"),
    ],
    "bank": [
        (2, "**Prelims Core** — Quant (DI, simplification), Reasoning puzzles, English RC"),
        (2, "**Mains** — advanced puzzles, caselet DI, banking awareness, descriptive writing"),
        (1, "**Mocks & Interview** — full-length mocks, current banking affairs"),
    ],
    "upsc_cse": [
        (3, "**NCERTs 6–12** — polity, history, geography, economy, science + newspaper habit"),
        (4, "**Standard Books** — Laxmikanth, Spectrum, GC Leong, Ramesh Singh; answer-writing begins"),
        (2, "**Optional Subject** — finish syllabus, notes"),
        (3, "**Prelims Sprint** — PYQs, 50+ prelims tests, CSAT practice"),
    ],
}

# ─────────────────────────────── Utilities ───────────────────────────────
def today() -> date:
    return datetime.now(IST).date()


def d(iso: str) -> date:
    return date.fromisoformat(iso)


def fmt(dt: date) -> str:
    return dt.strftime("%d/%m/%Y")


def add_months(dt: date, n: int) -> date:
    y, m = divmod(dt.month - 1 + n, 12)
    return date(dt.year + y, m + 1, 1)


def calculate_age_from_dob(dob_str: str, ref_date: Optional[date] = None) -> Optional[int]:
    """Calculates age in completed years as on ref_date (default today)."""
    if not dob_str:
        return None
    ref = ref_date or today()
    s = str(dob_str).strip()
    for fmt_str in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s, fmt_str).date()
            age = ref.year - dt.year - ((ref.month, ref.day) < (dt.month, dt.day))
            return max(0, age)
        except ValueError:
            continue
    m = re.search(r"\b(19\d{2}|20\d{2})\b", s)
    if m:
        yr = int(m.group(1))
        return max(0, ref.year - yr)
    if s.isdigit() and 10 <= int(s) <= 65:
        return int(s)
    return None


def status(job: Dict[str, Any]) -> Tuple[str, int]:
    """Returns (OPEN|UPCOMING|CLOSED, days_left_or_until)."""
    t, o, l = today(), d(job["opens"]), d(job["last_date"])
    if t < o:
        return "UPCOMING", (o - t).days
    if t <= l:
        return "OPEN", (l - t).days
    return "CLOSED", (t - l).days


def pwbd_allowed(job: Dict[str, Any]) -> bool:
    return job.get("physical", {}).get("pwbd_ok", True)


def age_relax(profile: Dict[str, Any], job: Dict[str, Any]) -> int:
    if not job["relax"]:
        return 0
    pwbd = PWBD_RELAX if profile.get("pwbd") and pwbd_allowed(job) else 0
    return CAT_RELAX.get(profile.get("category", "UR"), 0) + pwbd


def relax_label(profile: Dict[str, Any], job: Dict[str, Any]) -> str:
    parts = []
    cat = profile.get("category", "UR")
    if CAT_RELAX.get(cat):
        parts.append(f"{cat} +{CAT_RELAX[cat]}")
    if profile.get("pwbd") and pwbd_allowed(job):
        parts.append(f"PwBD +{PWBD_RELAX}")
    return " & ".join(parts)


def fee_for(profile: Dict[str, Any], job: Dict[str, Any]) -> str:
    if profile.get("pwbd"):
        return job["fee"]["SC"]  # PwBD fee concession mirrors SC/ST across SSC/UPSC/RRB/IBPS
    if profile.get("gender") == "F":
        return job["fee"]["F"]
    return job["fee"].get(profile.get("category", "UR"), job["fee"]["UR"])


# ─────────────────────────── Profile extraction ───────────────────────────
ASPIRATION = re.compile(
    r"(want|wish|plan\w*|pursu\w*|doing|studying|join\w*|enrol\w*|admission|should|which|what|"
    r"under|future|become|final year|appearing|dream|target)\W+(?:\w+\W+){0,3}$"
)
EDU_PATTERNS = [
    (5, r"\b(post ?graduat\w*|m\.?\s?tech|m\.?\s?sc|m\.?\s?com|mba|mca|m\.a\.?|ph\.?d|pg)\b"),
    (4, r"\b(graduat\w*|bachelor\w*|b\.?\s?tech|b\.e\.?|b\.?\s?sc|b\.?\s?com|b\.a\.?|\bba\b|bca|bba|be (?:civil|mech\w*|electr\w*|cse|it))\b"
        r"|(?:have|holding|completed|with) (?:a )?degree|degree (?:holder|pass\w*|complete\w*)"),
    (3, r"\b(diploma|polytechnic)\b"),
    (2, r"\b(12th|12 ?pass\w*|class ?12|10\s?\+\s?2|intermediate|inter pass|hsc|puc|higher secondary)\b"),
    (1, r"\b(10th|10 ?pass\w*|class ?10|matric\w*|high school|sslc|iti)\b"),
]
ENGG = re.compile(r"\b(b\.?\s?tech|b\.e\.?|engineer\w*|polytechnic|civil|mechanical|electrical|electronics|cse|ece|eee)\b")
NON_ENGG_DIPLOMA = re.compile(r"\b(d\.?\s?pharm|pharmacy|d\.?\s?el\.?\s?ed|nursing|hotel|fashion)\b")
NEGATION = r"(?:no|not|don'?t|do not|without|haven'?t|have not|dont)\W+(?:\w+\W+){0,2}"


def extract_profile(text: str) -> Dict[str, Any]:
    q = " " + text.lower() + " "
    p: Dict[str, Any] = {}

    # Age — explicit phrases first, then "NN years", then DOB
    m = (re.search(r"\b(?:i am|i'm|im|age|aged|age is|my age is)\s*:?\s*(\d{2})\b(?!\s*(?:%|wpm|posts?|marks|cm))", q)
         or re.search(r"\b(\d{2})\s*(?:years?|yrs?|y/?o)\b", q))
    if m and 14 <= int(m.group(1)) <= 45:
        p["age"] = int(m.group(1))
    else:
        dob = re.search(r"\b(?:born|dob|birth)\D{0,15}(?:(\d{1,2})[/\-.](\d{1,2})[/\-.])?((?:19|20)\d{2})\b", q)
        if dob:
            t = today()
            bd = date(int(dob.group(3)), int(dob.group(2) or 1), int(dob.group(1) or 1))
            p["age"] = t.year - bd.year - ((t.month, t.day) < (bd.month, bd.day))

    # Education — highest COMPLETED level; aspirational mentions become "pursuing"
    best = 0
    for level, pat in EDU_PATTERNS:
        for hit in re.finditer(pat, q):
            before = q[max(0, hit.start() - 40):hit.start()]
            if ASPIRATION.search(before):
                if re.search(r"(pursu|doing|studying|final year|appearing)", before):
                    p["pursuing"] = EDU[level]
                continue
            best = max(best, level)
            if level == 1 and "iti" in hit.group(0):
                p["iti"] = True
    if best:
        p["edu"] = best
        if best >= 3 and (ENGG.search(q) or (best == 3 and not NON_ENGG_DIPLOMA.search(q))):
            p["engg"] = True
    if re.search(r"\b(pcm|science|maths?|physics)\b", q):
        p["science"] = True

    # Category (strip b.sc/m.sc to avoid false "SC")
    cq = re.sub(r"\b[bm]\.?\s?sc\b", " ", q)
    c = re.search(r"\b(obc|sc|st|ews|ur|general|gen|unreserved)\b", cq)
    if c:
        p["category"] = {"general": "UR", "gen": "UR", "unreserved": "UR"}.get(c.group(1), c.group(1).upper())
    if re.search(r"\b(pwbd|pwd|divyang|disab\w*|handicap\w*)\b", q):
        p["pwbd"] = not re.search(NEGATION + r"(pwbd|pwd|disab)", q)
    if re.search(r"\b(female|woman|women|girl|lady)\b", q):
        p["gender"] = "F"
    elif re.search(r"\b(male|man|boy)\b", q):
        p["gender"] = "M"

    # Physical profile — height (cm or ft/in), driving licence, marital status
    h = re.search(r"\b(1[3-9]\d(?:\.\d)?)\s*cm\b", q)
    if h:
        p["height_cm"] = float(h.group(1))
    else:
        f = re.search(r"\b([4-6])\s*(?:'|ft|feet|foot)\s*(\d{1,2})?\s*(?:\"|''|in\b|inch\w*)?", q)
        if f:
            p["height_cm"] = round(int(f.group(1)) * 30.48 + int(f.group(2) or 0) * 2.54, 1)
    if re.search(r"\b(driving licen[cs]e|licen[cs]e|dl|lmv)\b", q):
        p["licence"] = not re.search(NEGATION + r"(driving|licen|dl|lmv)", q)
    if re.search(r"\b(unmarried|single|bachelor(?! ?'?s? degree)(?! of))\b", q):
        p["married"] = False
    elif re.search(r"\bmarried\b", q):
        p["married"] = True

    # Date of Birth (DOB) pattern
    dob_m = re.search(r"\b(?:dob|born|birth date|date of birth)\D{0,10}(\d{1,2}[/\-.]\d{1,2}[/\-.](?:19|20)\d{2}|(?:19|20)\d{2}[/\-.]\d{1,2}[/\-.]\d{1,2})\b", q)
    if dob_m:
        p["dob"] = dob_m.group(1).replace("-", "/").replace(".", "/")
        calced = calculate_age_from_dob(p["dob"])
        if calced is not None:
            p["age"] = calced

    # 12th / Qualifying marks %
    pct = re.search(r"\b([3-9]\d(?:\.\d+)?)\s*(?:%|percent|percentage|marks)\b", q)
    if pct:
        p["percentage"] = float(pct.group(1))

    # Gap years
    gap = re.search(r"\b([0-9]+)\s*(?:years?|yrs?)\s*gap\b|\bgap\s*(?:of)?\s*([0-9]+)\s*(?:years?|yrs?)?\b", q)
    if gap:
        p["gap_years"] = int(gap.group(1) or gap.group(2))

    # Sector preference
    if re.search(r"\b(railway|railways|rrb)\b", q):
        p["job_sector"] = "RAILWAY"
    elif re.search(r"\b(defence|defense|army|navy|air force|police|constable|uniform|nda|cds)\b", q):
        p["job_sector"] = "DEFENCE"
    elif re.search(r"\b(bank|banking|ibps|sbi|po)\b", q):
        p["job_sector"] = "BANKING"
    elif re.search(r"\b(clerk|clerical|chsl|deo|typist|office|ministerial)\b", q):
        p["job_sector"] = "CLERICAL"

    return p


def find_mentioned_job(text: str) -> Optional[Dict[str, Any]]:
    """Longest-matching alias wins (so 'upsc cds' → CDS, not CSE)."""
    q = " " + text.lower() + " "
    best, best_len = None, 0
    for job in fetch_vacancies():
        for a in job["aliases"]:
            if len(a) > best_len and re.search(r"\b" + re.escape(a) + r"\b", q):
                best, best_len = job, len(a)
    return best


def find_job_by_id(job_id: str) -> Optional[Dict[str, Any]]:
    for job in fetch_vacancies():
        if job["id"] == job_id or job.get("short", "").lower() == job_id.lower():
            return job
    return None


# ─────────────────────── Fitness / gender / category screener ───────────────────────
def fitness(p: Dict[str, Any], job: Dict[str, Any]) -> List[Dict[str, str]]:
    """Alerts: level ∈ block (disqualifies) | warn (verify before applying) | ok | info."""
    ph = job.get("physical")
    if not ph:
        return []
    g, out = p.get("gender"), []
    add = lambda lvl, txt: out.append({"level": lvl, "text": txt})

    if p.get("pwbd") and not ph.get("pwbd_ok", True):
        add("block", "Post not identified suitable for PwBD (combat/uniformed service)")
    if ph.get("gender_only") and g and g != ph["gender_only"]:
        add("block", f"Open to {GENDER[ph['gender_only']]} candidates only")
    if ph.get("gender_note"):
        add("info", ph["gender_note"])
    if ph.get("unmarried"):
        if p.get("married") is True:
            add("block", "Must be unmarried at entry and through training")
        else:
            add("info", "Must be unmarried at entry and through training")
    if ph.get("pcm_wings") and not p.get("science"):
        add("warn", "Without 12th Physics + Maths only Army/IMA/OTA wings are open (Navy/Air Force need PCM)")

    if ph.get("height"):
        st = p.get("category") == "ST" and ph.get("st_height")
        table = ph["st_height"] if st else ph["height"]
        tag = " (ST standard)" if st else ""
        if g and p.get("height_cm"):
            need = table[g]
            if p["height_cm"] < need:
                add("block", f"Height {p['height_cm']:g} cm < {need:g} cm PST cutoff for {GENDER[g]}{tag} — eligible only under regional relaxations")
            else:
                add("ok", f"Height {p['height_cm']:g} cm meets {need:g} cm cutoff{tag}")
        elif g:
            add("warn", f"PST height: min **{table[g]:g} cm** ({GENDER[g]}{tag}) — share your height to verify")
        else:
            add("warn", f"PST height: Male **{table['M']:g} cm** / Female **{table['F']:g} cm**{tag} — share gender & height")
    if ph.get("chest") and g != "F":
        add("info", ph["chest"])
    if ph.get("pet"):
        add("info", "PET: " + (ph["pet"][g] if g else f"Male {ph['pet']['M']} | Female {ph['pet']['F']}"))
    if ph.get("licence_male") and g != "F":
        if p.get("licence") is True:
            add("ok", "LMV driving licence ✔ — Delhi Police SI (Male) open to you")
        else:
            add("warn", "Delhi Police SI (Male) requires a valid **LMV (car) driving licence** by PET/PST date"
                        + (" — you said you don't have one: CAPF SI posts remain open" if p.get("licence") is False
                           else " — CAPF SI posts don't need it"))
    for n in ph.get("notes", []):
        add("info", n)
    return out


def assess(p: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    """Full per-post verdict: education, stream, age (with relaxation proof), fitness."""
    relax = age_relax(p, job)
    upper = job["max_age"] + relax
    edu_ok = p["edu"] >= job["min_edu"]
    strm_ok = job["stream"] != "engineering" or bool(p.get("engg"))
    age_ok = job["min_age"] <= p["age"] <= upper
    alerts = fitness(p, job)
    age_note = f"Age {p['age']} within {job['min_age']}–{job['max_age']}"
    if age_ok and p["age"] > job["max_age"]:
        age_note = (f"✅ Eligible **via {relax_label(p, job)} yrs relaxation**: age {p['age']} ≤ "
                    f"{job['max_age']} + {relax} = {upper}")
    elif not age_ok and p["age"] > upper:
        age_note = f"Over age: {p['age']} > {upper}" + ("" if job["relax"] else " (no category relaxation for this post)")
    elif not age_ok:
        age_note = f"Under age: minimum {job['min_age']}"
    cat = p.get("category", "UR")
    cert = CERT_NOTE.get(cat) if (cat != "UR") else None
    if p.get("pwbd") and pwbd_allowed(job):
        cert = (cert + " · " if cert else "") + "PwBD: ≥40% benchmark disability certificate (UDID)"

    # Percentage eligibility check (e.g. RRB NTPC requires min 50% for UR/OBC/EWS)
    if p.get("percentage") is not None and "rrb-ntpc" in job["id"]:
        if cat in ("UR", "OBC", "EWS") and p["percentage"] < 50:
            alerts.append({"level": "block", "text": f"12th marks {p['percentage']:g}% is below 50% cutoff required for {cat} in RRB NTPC (waived for SC/ST/PwBD)"})
        elif cat in ("UR", "OBC", "EWS"):
            alerts.append({"level": "ok", "text": f"12th marks {p['percentage']:g}% satisfies the 50% minimum eligibility bar"})

    # Gap years context
    if p.get("gap_years", 0) > 0:
        alerts.append({"level": "info", "text": f"Gap of {p['gap_years']} year(s): No negative penalty in Central Govt recruitment (SSC/RRB/UPSC)"})

    return {"edu_ok": edu_ok, "stream_ok": strm_ok, "age_ok": age_ok, "upper": upper, "relax": relax,
            "age_note": age_note, "cert": cert, "alerts": alerts,
            "blocked": any(a["level"] == "block" for a in alerts)}


# ───────────────────────────── Tier engines ─────────────────────────────
def matches_sector(job: Dict[str, Any], sector: str) -> bool:
    if not sector or sector == "ALL":
        return True
    fam = job.get("family", "")
    jid = job.get("id", "").lower()
    title = (job.get("title", "") + " " + job.get("short", "")).lower()
    if sector == "CLERICAL":
        return fam in ("ssc_12th", "rrb_undergrad", "ssc_grad") or any(w in title for w in ("chsl", "clerk", "deo", "typist", "assistant"))
    if sector == "RAILWAY":
        return fam in ("rrb_undergrad", "rrb_level1") or "rrb" in jid or "railway" in title
    if sector == "DEFENCE":
        return fam in ("defence_constable", "nda") or any(w in title for w in ("constable", "nda", "defence", "police", "cpo", "cds", "bsf", "crpf"))
    if sector == "BANKING":
        return fam in ("bank",) or "ibps" in jid or "sbi" in title or "bank" in title
    return True


def tier1(p: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    sec = p.get("job_sector", "ALL")
    for job in fetch_vacancies():
        a = assess(p, job)
        if a["edu_ok"] and a["stream_ok"] and a["age_ok"]:
            st, days = status(job)
            out.append({**job, "_status": st, "_days": days, "_a": a, "_sec_match": 0 if matches_sector(job, sec) else 1})
    order = {"OPEN": 0, "UPCOMING": 1, "CLOSED": 2}
    return sorted(out, key=lambda j: (j["_a"]["blocked"], j["_sec_match"], order[j["_status"]], j["rank"]))


def qualification_path(p: Dict[str, Any], job: Dict[str, Any]) -> Tuple[int, str]:
    have, need = p["edu"], job["min_edu"]
    if job["stream"] == "engineering" and not p.get("engg"):
        if have >= 2:
            lateral = p.get("science") or p.get("iti")
            return (2 if lateral else 3), (
                "Polytechnic Diploma in Engineering — 3 yrs"
                + (" (**2-yr lateral entry** with 12th PCM/ITI)" if lateral else "") + " · or B.Tech 4 yrs")
        return 3, "Polytechnic Diploma in Engineering after 10th — 3 yrs"
    if have >= need:
        return 0, ""
    if need == 2:
        return 2, "Complete 12th (regular or NIOS) — 2 yrs"
    if need == 3:
        return (3 if have == 1 else 2), "Polytechnic Diploma — 3 yrs (2-yr lateral entry after 12th PCM)"
    if need == 4:
        if have == 1:
            return 5, "12th (2 yrs) → any Bachelor's degree (3 yrs)"
        if have == 3:
            return 3, "B.Tech lateral entry or any Bachelor's degree — 3 yrs"
        return 3, "Any Bachelor's degree (B.A./B.Sc./B.Com) — 3 yrs, regular or UGC-DEB distance (IGNOU/state open univ.)"
    return 2, "Post-graduation — 2 yrs"


def tier2(p: Dict[str, Any], t1_ids: set) -> List[Dict[str, Any]]:
    out = []
    sec = p.get("job_sector", "ALL")
    for job in fetch_vacancies():
        if job["id"] in t1_ids or job.get("demo"):
            continue
        a = assess(p, job)
        if a["blocked"]:
            continue  # gender/PwBD/marital/height blocker — not a realistic ladder step
        years, path = qualification_path(p, job)
        eligible_age = max(p["age"] + years, job["min_age"])
        if eligible_age > a["upper"] or p["age"] > a["upper"]:
            continue
        if years == 0 and p["age"] >= job["min_age"]:
            continue
        out.append({**job, "_years": years, "_path": path or f"Age gap only — minimum age {job['min_age']}",
                    "_elig_age": eligible_age, "_elig_year": today().year + (eligible_age - p["age"]),
                    "_upper": a["upper"], "_window": a["upper"] - eligible_age + 1, "_a": a,
                    "_sec_match": 0 if matches_sector(job, sec) else 1})
    return sorted(out, key=lambda j: (j["_sec_match"], j["_elig_age"], j["rank"]))


def pick_primary(t1: List[Dict[str, Any]], t2: List[Dict[str, Any]],
                 mentioned: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if mentioned:
        for j in t1 + t2:
            if j["id"] == mentioned["id"]:
                return j
    live = [j for j in t1 if j["_status"] != "CLOSED" and not j["_a"]["blocked"]]
    live = [j for j in live if not j.get("demo")] or live
    return live[0] if live else (t1[0] if t1 else (t2[0] if t2 else None))


def roadmap(job: Dict[str, Any]) -> List[Dict[str, str]]:
    start, cursor, plan = today(), 0, []
    for months, focus in ROADMAPS[job["family"]]:
        a, b = add_months(start, cursor), add_months(start, cursor + months - 1)
        span = f"Month {cursor + 1}" + (f"–{cursor + months}" if months > 1 else "")
        cal = a.strftime("%b %Y") + ("" if months == 1 else f" – {b.strftime('%b %Y')}")
        plan.append({"span": span, "calendar": cal, "focus": focus})
        cursor += months
    return plan


# ───────────────────────────── Card builders (schema v3) ─────────────────────────────
ICON = {"block": "⛔", "warn": "⚠️", "ok": "✅", "info": "ℹ️"}


def url_action(label: str, url: str) -> Dict[str, str]:
    return {"type": "open_url", "label": label, "url": url}


def reply_action(label: str, message: str) -> Dict[str, str]:
    return {"type": "send_message", "label": label, "message": message}


def post_card(p: Dict[str, Any], j: Dict[str, Any], tier: int) -> Dict[str, Any]:
    st, days = status(j)
    a = j.get("_a") or (assess(p, j) if "age" in p and "edu" in p else None)
    fields = [
        {"label": "Organisation", "value": j["org"]},
        {"label": "Vacancies", "value": posts_txt(j)},
        {"label": "Pay", "value": j["pay"]},
        {"label": "Opens", "value": fmt(d(j["opens"]))},
        {"label": "Last date", "value": fmt(d(j["last_date"]))},
        {"label": "Age limit", "value": f"{j['min_age']}–{j['max_age']}" + (f" (yours: up to {a['upper']})" if a else "")},
        {"label": "Fee", "value": fee_for(p, j)},
        {"label": "Eligibility", "value": j["eligibility"]},
    ]
    badges = [st] + (["ELIGIBLE VIA RELAXATION"] if a and a["age_ok"] and p.get("age", 0) > j["max_age"] else []) \
        + (["PHYSICAL BLOCKER"] if a and a["blocked"] else []) + (["PET/PST"] if j.get("physical", {}).get("pet") else [])
    alerts = (a["alerts"] if a else fitness(p, j))
    if a and a["age_ok"] and p["age"] > j["max_age"]:
        alerts = [{"level": "ok", "text": re.sub(r"\*\*", "", a["age_note"])}] + alerts
    return {
        "id": j["id"], "kind": "post", "tier": tier, "title": j["short"], "subtitle": j["title"],
        "status": st, "days": days, "badges": badges, "fields": fields, "alerts": alerts,
        "actions": [url_action("🚀 Apply Now", j["apply_url"]), url_action("📄 Official PDF", j["pdf_url"]),
                    reply_action("📅 Roadmap", f"roadmap {j['short']}"),
                    reply_action("🔎 Full eligibility check", f"details {j['short']}")],
    }


# ───────────────────────────── Official ASI:One Interactive Cards ─────────────────────────────
def make_interactive_card_metadata(card_kind: str, card_payload: dict, drawer_width: str = "540",
                                   terminal: bool = False) -> dict:
    """Official ASI:One Interactive Cards wire format (card_protocol_version: 1). All values must be strings."""
    meta = {
        "card_protocol_version": "1",
        "requires_card_interaction": "true",
        "card_kind": card_kind,
        "card_payload": json.dumps(card_payload, ensure_ascii=False),
        "preferred_drawer_width_px": drawer_width,
    }
    if terminal:
        meta["is_terminal"] = "true"
    return meta


def build_profile_form() -> dict:
    """Card Kind: form — Asks candidate DOB, education, percentage, gap years, and target sector."""
    return {
        "title": "Setup Candidate Career Profile",
        "fields": [
            {
                "name": "dob",
                "kind": "text",
                "label": "Date of Birth (e.g. 15/08/2005 or 2005-08-15)",
                "required": False
            },
            {
                "name": "edu",
                "kind": "select",
                "label": "Highest Qualification",
                "options": [
                    {"value": "1", "label": "10th Pass (Matriculation)"},
                    {"value": "2", "label": "12th Pass (10+2 / Intermediate)"},
                    {"value": "3", "label": "Diploma / Polytechnic"},
                    {"value": "4", "label": "Graduate (Any Bachelor's Degree)"},
                    {"value": "5", "label": "Post-Graduate"}
                ]
            },
            {
                "name": "percentage",
                "kind": "number",
                "label": "12th / Qualifying Marks % (e.g. 68)",
                "required": False
            },
            {
                "name": "gap_years",
                "kind": "select",
                "label": "Gap Years after 10th / 12th (if any)",
                "options": [
                    {"value": "0", "label": "No Gap (0 Years)"},
                    {"value": "1", "label": "1 Year Gap"},
                    {"value": "2", "label": "2 Years Gap"},
                    {"value": "3", "label": "3+ Years Gap"}
                ]
            },
            {
                "name": "job_sector",
                "kind": "select",
                "label": "Target Job Sector / Category",
                "options": [
                    {"value": "ALL", "label": "All Central Govt (SSC, Railways, Defence, Banking)"},
                    {"value": "CLERICAL", "label": "Clerical / Ministerial (SSC CHSL, Clerk, DEO, Typist)"},
                    {"value": "RAILWAY", "label": "Railways (RRB NTPC, Group D, ALP, JE)"},
                    {"value": "DEFENCE", "label": "Defence & Uniformed (GD Constable, NDA, Police, CDS)"},
                    {"value": "BANKING", "label": "Banking & Insurance (IBPS, SBI, RBI)"}
                ]
            },
            {
                "name": "category",
                "kind": "select",
                "label": "Reservation Category",
                "options": [
                    {"value": "UR", "label": "UR (General / Unreserved)"},
                    {"value": "OBC", "label": "OBC (Non-Creamy Layer)"},
                    {"value": "EWS", "label": "EWS (Economically Weaker Section)"},
                    {"value": "SC", "label": "SC (Scheduled Caste)"},
                    {"value": "ST", "label": "ST (Scheduled Tribe)"}
                ]
            },
            {
                "name": "gender",
                "kind": "select",
                "label": "Gender",
                "options": [
                    {"value": "M", "label": "Male"},
                    {"value": "F", "label": "Female"}
                ]
            },
            {"name": "use_sample", "kind": "checkbox", "label": "🧪 Fill anything I leave empty with sample (dummy) data"},
        ],
        "submit_cta": {
            "label": "Check My Opportunities",
            "selection": {"action": "submit_profile"}
        }
    }


# Card artwork (github.com/stdioa9xh/documentpdf/cards): one design per sector, no official logos.
CARD_BASE = "https://raw.githubusercontent.com/stdioa9xh/documentpdf/main/cards"
CARD_IDS = {"ssc-chsl-2026", "rrb-ntpc-ug-2026", "ssc-gd-2027", "rrb-group-d-2026", "upsc-nda-i-2027", "ssc-mts-2026",
            "rrb-je-2026", "mpesb-sub-eng-2026", "ssc-cpo-si-2026", "ssc-cgl-2027", "ibps-po-xiv", "upsc-cds-i-2027",
            "upsc-cse-2027", "adrb-cgle-demo-2026", "adrb-mts-demo-2026", "adrb-constable-demo-2026",
            "adrb-ldc-demo-2026", "adrb-tech-demo-2026", "adrb-aso-demo-2026", "adrb-senior-asst-demo-2026",
            "adrb-research-demo-2026"}
SECTOR_CARD = [
    (re.compile(r"\b(nda|cds|afcat|army|navy|air force|agniveer)", re.I), "defence"),
    (re.compile(r"\b(police|constable|gd|capf|cpo|si\b|bsf|crpf|cisf)", re.I), "police"),
    (re.compile(r"\b(je|engineer)", re.I), "engg"),
    (re.compile(r"\b(rrb|railway)", re.I), "rail"),
    (re.compile(r"\b(ibps|sbi|bank|rbi)", re.I), "bank"),
    (re.compile(r"\bupsc\b", re.I), "upsc"),
    (re.compile(r"\b(state|psc|mpesb|vyapam)\b", re.I), "state"),
]


def job_image(j: dict) -> str:
    if j.get("id") in CARD_IDS:
        return f"{CARD_BASE}/{j['id']}.png"
    text = f"{j.get('short', '')} {j.get('title', '')} {j.get('org', '')}"
    return f"{CARD_BASE}/sector-{next((k for rx, k in SECTOR_CARD if rx.search(text)), 'ssc')}.png"


def build_jobs_carousel(p: dict, eligible_jobs: list) -> dict:
    """Card Kind: carousel — eligible posts, then tiles for the career ladder and the document check."""
    items = []
    for j in eligible_jobs[:8]:
        st, days = status(j)
        badges = [{"label": f"{days} days left" if st == "OPEN" else (f"opens in {days} days" if st == "UPCOMING" else st),
                   "variant": "warning" if st == "OPEN" and days <= 20 else ("success" if st == "OPEN" else "info")}]
        a = j.get("_a") or (assess(p, j) if "age" in p and "edu" in p else None)
        if a and a.get("blocked"):
            badges.append({"label": "Physical blocker", "variant": "warning"})
        elif a and p.get("age", 0) > j["max_age"] and a.get("age_ok"):
            badges.append({"label": f"Eligible via {p.get('category', 'UR')} relaxation", "variant": "success"})
        items.append({
            "id": j["id"],
            "image": job_image(j),
            "title": j["short"],
            "subtitle": f"{posts_txt(j)} posts · {j['pay']}",
            "badges": badges,
            "secondary_text": f"Fee: {fee_for(p, j)} · Age {j['min_age']}–{j['max_age']} · closes {fmt(d(j['last_date']))}",
            "primary_cta": {"label": "View post", "selection": {"action": "pick_job", "job_id": j["id"]}},
        })
    if "age" in p and "edu" in p:
        items.append({
            "id": "career_ladder", "image": f"{CARD_BASE}/career-ladder.png",
            "title": "Career ladder (Tier 2)", "subtitle": "Officer posts you can unlock next",
            "badges": [{"label": "Gap analysis", "variant": "info"}],
            "secondary_text": "Exact qualification gap, eligible age and attempts left",
            "primary_cta": {"label": "Show ladder", "selection": {"action": "show_ladder"}},
        })
    items.append({
        "id": "document_check", "image": f"{CARD_BASE}/document-check.png",
        "title": "Check my documents", "subtitle": "Class 10 vs Aadhaar name & DOB · category certificate",
        "badges": [{"label": "Gemini Vision", "variant": "success"}],
        "secondary_text": "Catches name mismatches before document verification and drafts the affidavit",
        "primary_cta": {"label": "Start check", "selection": {"action": "doc_checklist"}},
    })
    return {
        "title": "Posts you can apply for",
        "subtitle": f"Age {p.get('age', '—')} · {EDU.get(p.get('edu', 2), '12th Pass')} · {p.get('category', 'UR')}",
        "items": items
    }


def build_job_detail_card(p: dict, j: dict) -> dict:
    """Card Kind: detail — Focused view of a selected job."""
    st, days = status(j)
    a = assess(p, j) if ("age" in p and "edu" in p) else None
    return {
        "title": f"{j['short']} — {j['org']}",
        "hero_image": job_image(j),
        "summary_rows": [
            {"label": "Post & Hierarchy", "value": j["title"]},
            {"label": "Total Vacancies", "value": f"{posts_txt(j)} posts"},
            {"label": "Pay Matrix", "value": j["pay"]},
            {"label": "Application Deadline", "value": f"{fmt(d(j['last_date']))} ({days} days left)" if st == "OPEN" else fmt(d(j['last_date']))},
            {"label": "Application Fee", "value": fee_for(p, j)},
            {"label": "Eligibility Bar", "value": j["eligibility"]},
            {"label": "Your Status", "value": "Eligible ✅" if (a and a["edu_ok"] and a["stream_ok"] and a["age_ok"] and not a["blocked"]) else ("Blocked ⛔" if (a and a["blocked"]) else "Check Required")},
        ],
        "ctas": [
            {"label": "Check my documents", "primary": True, "selection": {"action": "audit_dossier", "job_id": j["id"]}},
            {"label": "5-Month Prep Roadmap", "selection": {"action": "roadmap", "job_id": j["id"]}},
            {"label": "Back to All Jobs", "selection": {"action": "show_carousel"}},
        ] + ([{"label": "✍️ Fill application form (Agent 2)", "selection": {"action": "fill_form", "job_id": j["id"]}}]
             if j.get("form_url") else [])
    }


def build_roadmap_card(p: dict, j: dict) -> dict:
    """Card Kind: detail — Month-by-month exam roadmap."""
    ready = add_months(today(), j["prep_months"])
    return {
        "title": f"Roadmap: {j['short']} ({j['prep_months']} Months)",
        "summary_rows": [
            {"label": "Target Post", "value": j["short"]},
            {"label": "Readiness Date", "value": f"Exam-ready by {ready.strftime('%b %Y')}"},
            {"label": "Application Deadline", "value": fmt(d(j['last_date']))},
            {"label": "Daily Parallel Skill", "value": j.get("skill", "General Awareness + Mock Tests")}
        ],
        "ctas": [
            {"label": "Check my documents", "primary": True, "selection": {"action": "audit_dossier", "job_id": j["id"]}},
            {"label": "Back to Job Details", "selection": {"action": "pick_job", "job_id": j["id"]}},
            {"label": "Back to All Jobs", "selection": {"action": "show_carousel"}}
        ]
    }


def build_document_form(p: dict) -> dict:
    """Card Kind: form — typed document details. Nothing is pre-filled or assumed."""
    jobs = [j for j in fetch_vacancies() if status(j)[0] != "CLOSED"][:10]
    return {
        "title": "Document check — type exactly what is printed",
        "fields": [
            {"name": "marksheet_name", "kind": "text", "label": "Full name on Class 10 marksheet"},
            {"name": "aadhaar_name", "kind": "text", "label": "Full name on Aadhaar card"},
            {"name": "marksheet_dob", "kind": "text", "label": "Date of birth on Class 10 (DD/MM/YYYY)"},
            {"name": "aadhaar_dob", "kind": "text", "label": "Date of birth on Aadhaar (DD/MM/YYYY)"},
            {"name": "father_name", "kind": "text", "label": "Father's / mother's name (for the affidavit)"},
            {"name": "cert_type", "kind": "select", "label": "Category certificate", "options": [
                {"value": "NONE", "label": "None / General (UR)"},
                {"value": "OBC-NCL", "label": "OBC (Non-Creamy Layer)"},
                {"value": "EWS", "label": "EWS (Income & Asset)"},
                {"value": "SC", "label": "SC"},
                {"value": "ST", "label": "ST"}]},
            {"name": "cert_issue_date", "kind": "text", "label": "Certificate issue date (DD/MM/YYYY)"},
        ] + ([{"name": "target_job", "kind": "select", "label": "Applying for",
               "options": [{"value": j["id"], "label": j["short"]} for j in jobs]}] if jobs else [])
        + [{"name": "use_sample", "kind": "checkbox", "label": "🧪 Fill anything I leave empty with sample (dummy) data"}],
        "submit_cta": {"label": "Run document check", "selection": {"action": "submit_documents"}},
    }


SEV_BADGE = {"ok": ("OK", "success"), "minor": ("Fixable", "warning"), "major": ("Action needed", "warning"),
             "critical": ("Critical", "warning")}
VERDICT_TEXT = {"VERIFIED": ("✅", "Documents consistent"), "ACTION_REQUIRED": ("⚠️", "Action required"),
                "CRITICAL_RISK": ("⛔", "Critical risk")}


def build_audit_card(name: str, result: dict, source: str) -> dict:
    """Card Kind: custom — verdict badges, one row per finding, and next-step buttons."""
    verdict = result["verdict"]
    rows = []
    for c in result["checks"]:
        label, variant = SEV_BADGE.get(c["severity"], ("Check", "info"))
        kids = [{"type": "group", "direction": "row", "gap": 8, "children": [
                    {"type": "badge", "label": label, "variant": variant},
                    {"type": "text", "value": c["field"], "style": "emphasis"}]},
                {"type": "text", "value": c["finding"], "style": "muted"}]
        if c.get("remedy"):
            kids.append({"type": "text", "value": "Fix: " + c["remedy"], "style": "body"})
        rows.append({"children": kids})
    buttons = []
    if result.get("affidavit_needed"):
        buttons.append({"type": "button", "label": "View affidavit draft", "primary": True,
                        "action": {"selection": {"action": "get_affidavit"}}})
    buttons += [{"type": "button", "label": "Edit my details", "action": {"selection": {"action": "doc_form"}}},
                {"type": "button", "label": "Back to posts", "action": {"selection": {"action": "show_carousel"}}}]
    return {"root": {"type": "section", "title": "Document check", "subtitle": f"{name} · {source}", "children": [
        {"type": "group", "direction": "row", "gap": 8, "children": [
            {"type": "badge", "label": VERDICT_TEXT[verdict][1], "variant": "success" if verdict == "VERIFIED" else "warning"},
            {"type": "badge", "label": f"Score {result['compliance_score']}/100", "variant": "info"},
            {"type": "badge", "label": "Rule engine", "variant": "info"}]},
        {"type": "text", "value": result["summary"], "style": "body"},
        {"type": "divider"},
        {"type": "heading", "value": "Findings", "level": 3},
        {"type": "list", "items": rows},
        {"type": "divider"},
        {"type": "group", "direction": "row", "gap": 8, "children": buttons},
    ]}}


def build_affidavit_card(docs: dict, gender: str) -> dict:
    """Card Kind: review — confirm the affidavit's details before printing."""
    rel = {"M": "S/o", "F": "D/o"}.get(gender, "S/o / D/o")
    return {
        "title": "Name affidavit — check before printing",
        "summary_rows": [
            {"label": "Deponent (Class 10 name)", "value": docs.get("marksheet_name", "")},
            {"label": "Name on Aadhaar", "value": docs.get("aadhaar_name", "")},
            {"label": "Parent", "value": f"{rel} {docs['father_name']}" if docs.get("father_name") else "Fill by hand"},
            {"label": "Fill by hand", "value": "Full address, place, date, Aadhaar last 4 digits"},
            {"label": "Attested by", "value": "Notary Public or Executive Magistrate"},
        ],
        "approve_cta": {"label": "Looks right — how to notarise", "primary": True,
                        "selection": {"action": "finalise_affidavit"}},
        "reject_cta": {"label": "Edit my details", "selection": {"action": "doc_form"}},
    }


AFFIDAVIT_STEPS = [
    "Print the draft on non-judicial stamp paper of the value your state prescribes.",
    "Fill in the blanks by hand: full address, place, date and the last 4 digits of Aadhaar.",
    "Sign in front of a Notary Public / Executive Magistrate; carry original Class 10 and Aadhaar.",
    "Fill the online form with the Class 10 name only.",
    "Keep the original affidavit plus 2 self-attested copies for document verification.",
]


def build_done_card() -> dict:
    """Card Kind: custom (terminal) — notarisation checklist."""
    return {"root": {"type": "section", "title": "Affidavit checklist", "subtitle": "Takes about an hour at a notary",
                     "children": [{"type": "list", "items": [
                         {"children": [{"type": "text", "value": f"{i}. {t}"}]} for i, t in enumerate(AFFIDAVIT_STEPS, 1)]}]}}


def build_ladder_card(p: dict, t2: list) -> dict:
    """Card Kind: custom — Tier 2 gaps grouped by the qualification that unlocks them; tap a post for details."""
    groups: Dict[str, list] = {}
    for j in t2:
        groups.setdefault(j["_path"], []).append(j)
    children: list = []
    for path, jobs in groups.items():
        children += [
            {"type": "heading", "value": path, "level": 3},
            {"type": "group", "direction": "row", "gap": 8, "children": [
                {"type": "badge", "label": f"Eligible at age {jobs[0]['_elig_age']} (~{jobs[0]['_elig_year']})", "variant": "info"},
                {"type": "badge", "label": f"{sum(j['posts'] for j in jobs):,}+ posts", "variant": "success"}]},
            {"type": "list", "items": [{
                "children": [{"type": "text", "value": f"{j['short']} · {j['pay']}", "style": "emphasis"},
                             {"type": "text", "value": f"Age window {j['_elig_age']}–{j['_upper']} → ~{j['_window']} attempt(s)",
                              "style": "muted"}],
                "action": {"selection": {"action": "pick_job", "job_id": j["id"]}}} for j in jobs]},
            {"type": "divider"}]
    if not children:
        children = [{"type": "text", "value": "You already meet the qualification bar for every higher post tracked."}]
    children.append({"type": "button", "label": "Back to posts", "action": {"selection": {"action": "show_carousel"}}})
    return {"root": {"type": "section", "title": "Career ladder (Tier 2)",
                     "subtitle": f"Starting from {EDU.get(p.get('edu', 2))}, age {p.get('age', '—')}", "children": children}}


def pack(markdown: str, card_kind: str, card_payload: dict, profile: Dict[str, Any], terminal: bool = False) -> Dict[str, Any]:
    return {
        "markdown": markdown,
        "metadata": make_interactive_card_metadata(card_kind, card_payload, terminal=terminal),
        "profile": profile
    }


def quick_md(quick: List[Dict[str, str]]) -> str:
    return "💬 **Quick Actions:** " + " · ".join(f"`{q['message']}`" for q in quick)


# ───────────────────────────── Response builders ─────────────────────────────
def profile_line(p: Dict[str, Any]) -> str:
    dob_part = f" (DOB: **{p['dob']}**)" if p.get("dob") else ""
    bits = [f"Age **{p.get('age', '—')}**{dob_part}",
            f"**{EDU.get(p.get('edu', 2), '12th Pass')}**" + (" (Engineering)" if p.get("engg") else "")]
    if p.get("percentage"):
        bits.append(f"**{p['percentage']:g}%** marks")
    if p.get("gap_years"):
        bits.append(f"Gap: **{p['gap_years']} yr(s)**")
    bits.append(f"Category **{p.get('category', 'UR')}**")
    if p.get("job_sector") and p["job_sector"] != "ALL":
        bits.append(f"Target: **{p['job_sector'].capitalize()}**")
    if p.get("gender"):
        bits.append(GENDER.get(p["gender"], p["gender"]))
    if p.get("pwbd"):
        bits.append("PwBD")
    if p.get("height_cm"):
        bits.append(f"{p['height_cm']:g} cm")
    if p.get("licence"):
        bits.append("LMV licence")
    if p.get("pursuing"):
        bits.append(f"pursuing {p['pursuing']}")
    return " · ".join(bits)


def alerts_md(alerts: List[Dict[str, str]], limit: int = 4) -> List[str]:
    ranked = sorted(alerts, key=lambda a: ["block", "warn", "ok", "info"].index(a["level"]))
    return [f"   {ICON[a['level']]} {a['text']}" for a in ranked[:limit]]


def post_md(p: Dict[str, Any], j: Dict[str, Any], i: int) -> List[str]:
    a = j["_a"]
    when = (f"⏰ closes **{fmt(d(j['last_date']))}** ({j['_days']} days left)" if j["_status"] == "OPEN"
            else f"🗓️ opens {fmt(d(j['opens']))} · closes {fmt(d(j['last_date']))}")
    head = f"**{i}. {j['title']}** — `{j['_status']}`" + (" `⛔ BLOCKED`" if a["blocked"] else "")
    L = [head,
         f"   🏛️ {j['org']} · 👥 {posts_txt(j)} posts · 💼 {j['pay']}",
         f"   {when} · 💰 Fee: {fee_for(p, j)}",
         f"   🎂 {a['age_note']}" if p["age"] > j["max_age"] else f"   🎂 {j['min_age']}–{a['upper']} yrs (your limit)",
         f"   🎓 {j['eligibility']}"]
    L += alerts_md(a["alerts"])
    L += [f"   [🚀 Apply Now]({j['apply_url']}) · [📄 Official PDF]({j['pdf_url']}) · 💬 `details {j['short']}`", ""]
    return L


def build_assessment(p: Dict[str, Any], mentioned: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    t1 = tier1(p)
    t2 = tier2(p, {j["id"] for j in t1})
    t1 = featured_first(t1)
    primary = pick_primary(t1, t2, mentioned)
    cards: List[Dict[str, Any]] = []
    L: List[str] = ["## 🧭 ApplyEase 3-Tier Career Assessment", f"👤 {profile_line(p)}", ""]

    # Tier 1
    L.append("### ✅ Tier 1 — Apply Now (eligible today)")
    live = [j for j in t1 if j["_status"] != "CLOSED" and not j["_a"]["blocked"]]
    blocked = [j for j in t1 if j["_status"] != "CLOSED" and j["_a"]["blocked"]]
    if not live:
        L += ["_No open/upcoming post matches your exact profile right now._", ""]
    for i, j in enumerate(live[:6], 1):
        L += post_md(p, j, i)
        cards.append(post_card(p, j, 1))
    for j in blocked:
        why = next(a["text"] for a in j["_a"]["alerts"] if a["level"] == "block")
        L.append(f"⛔ **{j['short']}** — eligible on age/qualification, but blocked: {why} · 💬 `details {j['short']}`")
        cards.append(post_card(p, j, 1))
    if blocked:
        L.append("")
    missed = [j for j in t1 if j["_status"] == "CLOSED"]
    if missed:
        L += ["↩️ Recently closed (prepare for next cycle): " + ", ".join(
            j["short"] + (f" — next ~{j['next_cycle']}" if j.get("next_cycle") else "") for j in missed), ""]

    # Tier 2
    L.append("### 🪜 Tier 2 — Career Ladder & Gap Analysis")
    if not t2:
        L.append("_You already meet the qualification bar for every higher post tracked — focus on Tier 1 & Tier 3._")
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for j in t2:
        groups.setdefault(j["_path"], []).append(j)
    for path, jobs in groups.items():
        unlock = sum(j["posts"] for j in jobs)
        L += [f"🎯 **Gap → {path}**",
              f"   Start today → eligible at **age {jobs[0]['_elig_age']} (~{jobs[0]['_elig_year']})** · unlocks {unlock:,}+ posts/cycle:"]
        for j in jobs:
            warn = next((a["text"] for a in j["_a"]["alerts"] if a["level"] == "warn"), None)
            L.append(f"   • **{j['short']}** ({j['pay']}) — needs {EDU[j['min_edu']]}"
                     + (" in Engineering" if j["stream"] == "engineering" else "")
                     + f"; age window {j['_elig_age']}–{j['_upper']} → ~{j['_window']} attempt(s)"
                     + (f"\n     ⚠️ {warn}" if warn else ""))
        L.append("")
        cards.append({"id": f"ladder-{len(cards)}", "kind": "ladder", "tier": 2, "title": f"Gap → {path}",
                      "subtitle": f"Eligible at age {jobs[0]['_elig_age']} (~{jobs[0]['_elig_year']})",
                      "fields": [{"label": j["short"], "value": f"{j['pay']} · window {j['_elig_age']}–{j['_upper']}"} for j in jobs],
                      "alerts": [], "actions": [url_action(f"📄 {j['short']} PDF", j["pdf_url"]) for j in jobs[:3]]})

    # Tier 3
    L.append("### 📅 Tier 3 — Preparation Roadmap (starting today)")
    if primary:
        plan = roadmap(primary)
        ready = add_months(today(), primary["prep_months"])
        L.append(f"🎯 Primary target: **{primary['short']}** · est. prep **{primary['prep_months']} months** → exam-ready by **{ready.strftime('%b %Y')}**")
        if primary.get("_status") == "OPEN":
            L.append(f"⚠️ Step 0: submit the form before **{fmt(d(primary['last_date']))}** — preparation continues after applying.")
        L += [f"- **{s['span']}** ({s['calendar']}): {s['focus']}" for s in plan]
        if primary.get("skill") and primary["skill"] != "—":
            L.append(f"- 🏃/⌨️ Skill/physical track (daily, parallel): {primary['skill']}")
        cards.append({"id": f"roadmap-{primary['id']}", "kind": "roadmap", "tier": 3, "title": f"Roadmap: {primary['short']}",
                      "subtitle": f"{primary['prep_months']} months · ready by {ready.strftime('%b %Y')}",
                      "fields": [{"label": f"{s['span']} · {s['calendar']}", "value": re.sub(r"\*\*", "", s["focus"])} for s in plan],
                      "alerts": [], "actions": [url_action("🚀 Apply Now", primary["apply_url"])]})
    if p["edu"] < 4 and t2:
        grad_jobs = [j for j in t2 if j["min_edu"] >= 3]
        if grad_jobs:
            best_path = max(groups, key=lambda k: sum(j["posts"] for j in groups[k] if j["min_edu"] >= 3))
            first = min((j for j in grad_jobs if j["_path"] == best_path), key=lambda j: j["_elig_age"])
            L.append(f"\n🔀 **Parallel track:** enrol this academic session in **{first['_path'].split(' — ')[0]}**. "
                     f"Clear a Tier-1 job now, keep studying (distance mode is valid for SSC/UPSC), and by age "
                     f"**{first['_elig_age']}** you unlock Level-6/7+ posts like {', '.join(j['short'] for j in grad_jobs[:4])} "
                     f"({sum(j['posts'] for j in grad_jobs):,}+ vacancies in the current cycle alone).")

    # Contextual quick replies
    quick: List[Dict[str, str]] = []
    if not p.get("gender"):
        quick.append(reply_action("Add gender", "I am female"))
    if not p.get("height_cm") and any(j.get("physical", {}).get("height") for j in t1 + t2):
        quick.append(reply_action("Add height", "my height is 170 cm"))
    if "category" not in p:
        quick.append(reply_action("Add category", "I am OBC"))
    for j in live[:2]:
        quick.append(reply_action(f"{j['short']} details", f"details {j['short']}"))
    quick.append(reply_action("Start over", "reset"))

    carousel_card = build_jobs_carousel(p, live or t1)
    return pack("\n".join(L), "carousel", carousel_card, p)


def build_post_detail(p: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    notice = read_notification(job)
    if notice:
        job = apply_notice(job, notice["facts"])
    st, days = status(job)
    full = "age" in p and "edu" in p
    a = assess(p, job) if full else None
    L = [f"## 📋 {job['title']} — `{st}`",
         f"🏛️ {job['org']} · 👥 **{posts_txt(job)}** posts · 💼 {job['pay']}",
         f"🗓️ {fmt(d(job['opens']))} → **{fmt(d(job['last_date']))}**"
         + (f" ({days} days left)" if st == "OPEN" else ""),
         f"🎓 {job['eligibility']}",
         f"🎂 Age {job['min_age']}–{job['max_age']} · relaxation: "
         + ("OBC +3 · SC/ST +5 · PwBD +10 (additive)" if job["relax"] else "**none** (fixed DOB window)"),
         f"💰 Fee: Gen/OBC/EWS {job['fee']['UR']} · SC/ST {job['fee']['SC']} · Women {job['fee']['F']}", ""]
    if a:
        paper_ok = a["edu_ok"] and a["stream_ok"] and a["age_ok"]
        verdict = ("✅ You are ELIGIBLE" if paper_ok and not a["blocked"] else
                   "⛔ Eligible on paper — blocked at screening" if paper_ok else "❌ Not eligible yet")
        L += [f"### {verdict} — {profile_line(p)}",
              f"- {'✅' if a['edu_ok'] and a['stream_ok'] else '❌'} Qualification: {EDU[p['edu']]}"
              + ("" if a["stream_ok"] else " — engineering stream required"),
              f"- {'' if a['age_note'].startswith('✅') else ('✅ ' if a['age_ok'] else '❌ ')}{a['age_note']}",
              f"- 💰 Your fee: **{fee_for(p, job)}**"]
        if a["cert"]:
            L.append(f"- 📑 Documents: {a['cert']}")
        if not (a["edu_ok"] and a["stream_ok"]):
            yrs, path = qualification_path(p, job)
            L.append(f"- 🪜 Gap: {path} → eligible at age {max(p['age'] + yrs, job['min_age'])}")
        L.append("")
    alerts = a["alerts"] if a else fitness(p, job)
    if alerts:
        L.append("### 🏃 Physical / Gender / Medical Screening")
        L += [f"- {x[3:]}" for x in alerts_md(alerts, limit=12)]
        L.append("")
    if notice:
        L += notice_markdown(p, job, notice)
    else:
        L.append("### 📑 Documents you'll need")
        L += [f"- {DOC_TYPES[x]} — {DOC_WHY[x]}" for x in required_docs(p, job)]
    L.append("_Tap **Check my documents** to upload them — each one is read with Gemini Vision._")
    if job.get("form_url"):
        L.append("_Then **✍️ Fill application form (Agent 2)** — the Form Filler agent completes the official form._")
    L.append("")
    L.append(f"[🚀 Apply Now]({job['apply_url']}) · [📄 Official Notification PDF]({job['pdf_url']})")
    detail_card = build_job_detail_card(p, job)
    return pack("\n".join(L), "detail", detail_card, p)


def build_roadmap(p: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    plan = roadmap(job)
    ready = add_months(today(), job["prep_months"])
    L = [f"## 📅 {job['short']} — {job['prep_months']}-Month Roadmap (from today)",
         f"Exam-ready by **{ready.strftime('%b %Y')}** · form deadline **{fmt(d(job['last_date']))}**", ""]
    L += [f"- **{s['span']}** ({s['calendar']}): {s['focus']}" for s in plan]
    if job.get("skill") and job["skill"] != "—":
        L.append(f"- 🏃/⌨️ Daily parallel track: {job['skill']}")
    L += ["", f"[🚀 Apply Now]({job['apply_url']}) · [📄 Syllabus in Official PDF]({job['pdf_url']})"]
    roadmap_card = build_roadmap_card(p, job)
    return pack("\n".join(L), "detail", roadmap_card, p)


WELCOME = (
    "👋 **ApplyEase — Govt Career Intelligence Agent**\n\n"
    "Fill in your details in the card below or send your **Date of Birth** & **qualification** (e.g. *'DOB 15/08/2005, 12th pass, 68% marks, UR'*).\n"
    "I will automatically calculate your exact age, verify 12th percentage bars, factor in gap years, and return:\n"
    "1. ✅ **Tier 1** — posts you are eligible for today with direct apply links & fees\n"
    "2. 🪜 **Tier 2** — career ladder gap analysis (future officer posts at age 21)\n"
    "3. 📅 **Tier 3** — month-by-month preparation plan starting today\n"
    "4. 📑 **Document check (Gemini Vision)** — Class 10 vs Aadhaar name/DOB, certificate validity, affidavit draft\n"
    "5. ✍️ **Application form filled by Agent 2 · Form Filler** — it asks you only for what is missing\n\n"
    "🧪 *Just testing? Type **`demo`** to load a sample candidate, or tick **Use sample data** in any card.*\n\n"
)


def welcome(prefix: str = "") -> Dict[str, Any]:
    form_card = build_profile_form()
    return pack(prefix + WELCOME, "form", form_card, {})


DETAIL_INTENT = re.compile(r"\b(details?|eligib\w*|am i|can i|physical|height|pet|pst|licen[cs]e|fee|info|criteria)\b")
ROADMAP_INTENT = re.compile(r"\b(roadmap|prepar\w*|strategy|study plan|timeline|syllabus)\b")
AUDIT_INTENT = re.compile(r"\b(audit|verif\w*|documents?|docs|certificates?|marksheets?|aadhaa?r|affidavit|caste cert\w*)\b")
JOB_LIST_INTENT = re.compile(r"\b(jobs?|vacanc\w*|posts?|naukri|eligible|eligibility|apply|options?|opportunit\w*)\b")


DOC_KEYS = ("marksheet_name", "aadhaar_name", "father_name", "marksheet_dob", "aadhaar_dob",
            "cert_type", "cert_issue_date", "target_job")
AADHAAR_KW = r"(?:aadhaa?r|adhaa?r)(?:\s*card)?"
MARKSHEET_KW = (r"(?:class\s*10(?:th)?|10th|matric\w*|high\s*school|secondary)(?:\s*(?:mark\s*sheet|marksheet|certificate|cert))?"
                r"|mark\s*sheet|marksheet")
DATE_RX = r"(\d{1,2}[/\-.]\d{1,2}[/\-.](?:19|20)\d{2}|(?:19|20)\d{2}-\d{1,2}-\d{1,2})"
NAME_VERBS = re.compile(r"^(?:\s*(?:name|card|is|are|:|=|-|has|says|shows|reads|as|mein|me|par|hai|naam|ka|ki|written|printed|on|in)\b\s*|\s*[:=\-]\s*)+", re.I)
NAME_STOP = {"dob", "born", "date", "and", "but", "with", "while", "whereas", "aadhaar", "aadhar", "adhaar", "marksheet",
             "certificate", "cert", "obc", "ews", "father", "issued", "dated", "on", "in", "my", "which", "where", "has",
             "is", "pass", "passed", "the", "for", "hai", "aur", "lekin", "galat", "wrong", "different", "alag",
             "sahi", "correct", "spelling", "mismatch", "naam", "name"}


def _name_after(rest: str, explicit: bool) -> Optional[str]:
    rest = NAME_VERBS.sub("", rest.lstrip(" '\"")).strip(" '\"")
    words = []
    for w in re.findall(r"[A-Za-z][A-Za-z.]*|[^A-Za-z\s]", rest):
        if not re.match(r"[A-Za-z]", w) or w.lower().strip(".") in NAME_STOP:
            break
        words.append(w)
        if len(words) == 6:
            break
    if len(words) >= 2 or (words and explicit and len(words[0]) > 1):
        return " ".join(x.capitalize() if not re.fullmatch(r"[A-Za-z]\.?", x) else x.upper() for x in words)
    return None


def extract_docs(text: str) -> Dict[str, str]:
    """Reads document details from free text, e.g. 'marksheet says Rajesh Kumar Sharma, aadhaar has Rajesh K Sharma,
    aadhaar dob 14/08/2002, OBC certificate issued 15/01/2023'. Returns only what was actually stated."""
    out: Dict[str, str] = {}
    last_doc = None
    for clause in re.split(r"[;\n]|,|\bbut\b|\bwhile\b|\bwhereas\b|\blekin\b|\band\b|\baur\b", text, flags=re.I):
        low = clause.lower()
        cert = re.search(r"\b(obc(?:[\s-]*ncl)?|ews|sc|st|caste)\b[^.]*?\b(certificate|cert)\b|\b(certificate|cert)\b[^.]*?\b(obc|ews|sc|st)\b", low)
        if cert:
            kind = (cert.group(1) or cert.group(4) or "").split()[0].split("-")[0]
            if kind in ("obc", "ews", "sc", "st"):
                out["cert_type"] = {"obc": "OBC-NCL", "ews": "EWS"}.get(kind, kind.upper())
            dt = re.search(DATE_RX, clause)
            if dt:
                out["cert_issue_date"] = dt.group(1)
            continue
        f = re.search(r"\b(?:father|mother)'?s?(?:\s*name)?\b(.*)", clause, re.I)
        if f:
            nm = _name_after(f.group(1), True)
            if nm:
                out["father_name"] = nm
            continue
        for key, kw in (("aadhaar", AADHAAR_KW), ("marksheet", MARKSHEET_KW)):
            m = re.search(rf"(name\s+(?:on|in|as per|according to)\s+(?:my\s+|the\s+)?)?\b(?:{kw})\b(?P<rest>.*)", clause, re.I)
            if m:
                last_doc = key
                nm = _name_after(m.group("rest"), bool(m.group(1)) or "name" in low)
                if nm:
                    out.setdefault(f"{key}_name", nm)
                break
        dob = re.search(r"\b(?:dob|d\.o\.b\.?|date of birth|birth ?date|born)\b\D{0,6}" + DATE_RX, clause, re.I)
        if dob and last_doc:
            out.setdefault(f"{last_doc}_dob", dob.group(1))
    return out


def doc_request(p: Dict[str, Any]) -> Dict[str, Any]:
    """Inputs for run_dossier_audit, built only from what the candidate gave."""
    docs = p.get("docs", {})
    job = find_job_by_id(docs.get("target_job") or p.get("last_job") or "")
    cert = (docs.get("cert_type") or "").upper()
    category = p.get("category") or {"OBC-NCL": "OBC", "EWS": "EWS", "SC": "SC", "ST": "ST"}.get(cert, "UR")
    return {
        "marksheet_name": docs.get("marksheet_name", ""), "aadhaar_name": docs.get("aadhaar_name", ""),
        "father_name": docs.get("father_name", ""), "gender": p.get("gender", ""), "age": int(p.get("age") or 0),
        "marksheet_dob": docs.get("marksheet_dob", ""), "aadhaar_dob": docs.get("aadhaar_dob", ""),
        "category": category, "cert_type": cert, "cert_issue_date": docs.get("cert_issue_date", ""),
        "target_job": job["short"] if job else "", "closing_date": job["last_date"] if job else "",
        "missing_docs": "; ".join(DOC_TYPES[x] for x in required_docs(p, job)
                                  if x not in p.get("upload", {}).get("files", {})) if p.get("upload", {}).get("files") else "",
        "cert_central": docs.get("cert_central", ""),
    }


READ_PROMPT = """You read Indian documents uploaded by a government-job applicant. Identify the document and copy
fields EXACTLY as printed (English text; if only Hindi is printed, transliterate). Return ONLY a JSON object:
{"doc_type": one of "class10","class12","diploma","degree","aadhaar","obc_ncl","ews","sc_st","pwbd","other",
 "name": candidate's full name, "father_name": father's name or "", "mother_name": mother's name or "",
 "dob": date of birth as DD/MM/YYYY or "", "issue_date": certificate issue date as DD/MM/YYYY or "",
 "category": "OBC","EWS","SC","ST" or "", "central_format": true if an OBC/EWS certificate says it is for appointment
 to posts under the Government of India, false if it is a State-format certificate, null if unclear,
 "valid_fy": financial year an EWS certificate is valid for (e.g. "2026-27") or "",
 "aadhaar_last4": ONLY the last 4 digits of an Aadhaar number, never more,
 "readable": true/false, "quality_issue": "" or a short problem such as "blurry", "cropped", "glare"}
Never guess: use "" when a field is not visible. The applicant was asked to upload: """

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


def sniff_mime(blob: bytes, declared: str) -> str:
    if declared in ALLOWED_MIME:
        return declared
    if blob[:4] == b"%PDF":
        return "application/pdf"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return ""


def clean_fields(g: Dict[str, Any]) -> Dict[str, Any]:
    """Keeps only well-formed values; dates re-formatted, Aadhaar reduced to 4 digits."""
    out: Dict[str, Any] = {}
    for k in ("name", "father_name", "mother_name"):
        v = re.sub(r"[^A-Za-z .'-]", "", str(g.get(k) or "")).strip()
        if 2 <= len(v) <= 80:
            out[k] = re.sub(r"\s+", " ", v)
    for k in ("dob", "issue_date"):
        dt = parse_any_date(str(g.get(k) or ""))
        if dt:
            out[k] = _dmy(dt)
    if str(g.get("category") or "").upper() in ("OBC", "EWS", "SC", "ST"):
        out["category"] = str(g["category"]).upper()
    if isinstance(g.get("central_format"), bool):
        out["central_format"] = g["central_format"]
    if re.fullmatch(r"\d{4}-\d{2}", str(g.get("valid_fy") or "")):
        out["valid_fy"] = g["valid_fy"]
    last4 = re.sub(r"\D", "", str(g.get("aadhaar_last4") or ""))[-4:]
    if len(last4) == 4:
        out["aadhaar_last4"] = last4
    return out


class GeminiQuotaError(RuntimeError):
    pass


def gemini_post(key: str, body: Dict[str, Any], timeout: float, budget: Optional[float] = None) -> Dict[str, Any]:
    """generateContent with model fallback: on 429 (quota), 404 (model not available) or a timeout the next model
    is tried while the time budget lasts. Raises GeminiQuotaError with each model's reason when all fail."""
    models = list(dict.fromkeys([GEMINI_MODEL] + GEMINI_FALLBACK_MODELS))
    payload = json.dumps(body).encode("utf-8")
    deadline = time.time() + (budget or timeout)
    reasons = []
    for model in models:
        left = deadline - time.time()
        if left < 5:
            reasons.append(f"{model}: skipped (time budget used)")
            continue
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=payload, method="POST", headers={"Content-Type": "application/json", "x-goog-api-key": key})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=min(timeout, left)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            data["_model"] = model
            return data
        except urllib.error.HTTPError as e:
            if e.code not in (429, 404):
                raise
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            reasons.append(f"{model}: {e.code} {msg[:140]}")
        except (TimeoutError, OSError) as e:  # socket timeouts surface as TimeoutError / URLError(timeout)
            reasons.append(f"{model}: no answer in {time.time() - t0:.0f}s ({type(e).__name__})")
    raise GeminiQuotaError("Gemini quota/model unavailable — " + " | ".join(reasons))


def shrink_image(blob: bytes, mime: str) -> Tuple[bytes, str]:
    """Downscales big photos to MAX_IMAGE_SIDE (JPEG) when Pillow is available; otherwise returns them unchanged."""
    if not mime.startswith("image/") or len(blob) < 400_000:
        return blob, mime
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(blob))
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=85)
        return (out.getvalue(), "image/jpeg") if out.tell() < len(blob) else (blob, mime)
    except Exception:
        return blob, mime


def gemini_read(blob: bytes, mime: str, expected: str) -> Dict[str, Any]:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("Gemini isn't configured on Agent 2 (add the GEMINI_API_KEY secret)")
    body = {"contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": mime, "data": base64.b64encode(blob).decode()}},
                {"text": READ_PROMPT + (DOC_TYPES.get(expected) or "any document")}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 2048, "responseMimeType": "application/json"}}
    data = gemini_post(key, body, GEMINI_VISION_TIMEOUT_S, GEMINI_VISION_BUDGET_S)
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    g = json.loads(text)
    g = g if isinstance(g, dict) else {}
    g["_model"] = data.get("_model", "")
    return g


def read_document(ref: str, expected: str) -> Dict[str, Any]:
    blob, declared = storage_download(None, ref)
    mime = sniff_mime(blob, declared)
    if not mime:
        return {"ok": False, "doc_type": "", "fields": {}, "quality": "",
                "detail": f"unsupported file type ({declared or 'unknown'}) — send a JPG, PNG or PDF"}
    size_in = len(blob)
    blob, mime = shrink_image(blob, mime)
    g = gemini_read(blob, mime, expected)
    doc_type = g.get("doc_type") if g.get("doc_type") in DOC_TYPES else "other"
    fields = clean_fields(g)
    quality = str(g.get("quality_issue") or "")[:80] if g.get("readable", True) else (
        str(g.get("quality_issue") or "") or "not readable")[:80]
    ok = doc_type != "other" and bool(fields)
    return {"ok": ok, "doc_type": doc_type, "fields": fields, "quality": quality, "model": g.get("_model", ""),
            "bytes": f"{size_in // 1024}→{len(blob) // 1024} KB",
            "detail": "read" if ok else ("this doesn't look like one of the listed documents" if doc_type == "other"
                                         else "no fields could be read")}


NOTICE_PROMPT = """Read this recruitment notification and return ONLY JSON:
{"exam": str, "advertisement_no": str, "application_closes": "DD/MM/YYYY", "age_min": int, "age_max": int,
 "age_cutoff_date": "DD/MM/YYYY", "relaxation": {"OBC": int, "SC": int, "ST": int, "EWS": int},
 "min_education": one of "10th","12th","diploma","graduate","postgraduate", "total_vacancies": int,
 "required_documents": [{"document": str, "for": "all" | "OBC" | "EWS" | "SC/ST" | "PwBD" | "conditional"}],
 "application_form_url": str, "form_fields": [str]}"""
EDU_CODE = {"10th": 1, "12th": 2, "diploma": 3, "graduate": 4, "postgraduate": 5}
_NOTICE: Dict[str, Dict[str, Any]] = {}  # job id → facts read from its notification (read once per agent run)


def _http_bytes(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ApplyEase-Agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(15 * 1024 * 1024)


def _facts_from_json(n: Dict[str, Any]) -> Dict[str, Any]:
    rel = n.get("age", {}).get("relaxation_years", {})
    return {"exam": n.get("exam"), "advertisement_no": n.get("advertisement_no"),
            "application_closes": str(n.get("dates", {}).get("application_closes", ""))[:10],
            "age_min": n.get("age", {}).get("min"), "age_max": n.get("age", {}).get("max"),
            "age_cutoff_date": n.get("age", {}).get("cutoff_date"),
            "relaxation": {"OBC": rel.get("OBC (Non-Creamy Layer)", 0), "SC": rel.get("SC", 0), "ST": rel.get("ST", 0), "EWS": 0},
            "min_education": "graduate", "total_vacancies": n.get("total_vacancies"),
            "required_documents": [{"document": d["document"], "for": d["for"]} for d in n.get("required_documents", [])],
            "application_form_url": n.get("application_form_url"), "form_fields": n.get("application_form_fields", [])}


def read_notification(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Gemini reads the job's official notification PDF once; the structured JSON copy is the fallback."""
    if not job.get("notice_json"):
        return None
    if job["id"] in _NOTICE:
        return {**_NOTICE[job["id"]], "cached": True}
    t0, res = time.time(), None
    if os.getenv("GEMINI_API_KEY"):
        try:
            pdf = _http_bytes(job["pdf_url"])
            data = gemini_post(os.getenv("GEMINI_API_KEY"), {
                "contents": [{"parts": [{"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf).decode()}},
                                        {"text": NOTICE_PROMPT}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 4096, "responseMimeType": "application/json"}},
                GEMINI_VISION_TIMEOUT_S, GEMINI_VISION_BUDGET_S)
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            res = {"facts": json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip())), "source": "gemini",
                   "model": data.get("_model", ""), "secs": round(time.time() - t0, 1)}
        except Exception:
            res = None
    if res is None:
        try:
            res = {"facts": _facts_from_json(json.loads(_http_bytes(job["notice_json"]).decode("utf-8"))),
                   "source": "structured copy", "model": "", "secs": round(time.time() - t0, 1)}
        except Exception:
            return None
    _NOTICE[job["id"]] = res
    return {**res, "cached": False}


def apply_notice(job: Dict[str, Any], facts: Dict[str, Any]) -> Dict[str, Any]:
    """The job's rules as read from its notification (validated; anything implausible keeps the curated value)."""
    j = dict(job)
    lo, hi = facts.get("age_min"), facts.get("age_max")
    if isinstance(lo, int) and isinstance(hi, int) and 14 <= lo < hi <= 60:
        j["min_age"], j["max_age"] = lo, hi
    if facts.get("min_education") in EDU_CODE:
        j["min_edu"] = EDU_CODE[facts["min_education"]]
    return j


def notice_markdown(p: Dict[str, Any], job: Dict[str, Any], n: Dict[str, Any]) -> List[str]:
    f = n["facts"]
    how = ("cached from earlier read" if n.get("cached") else
           f"read just now with Gemini ({n['model']}) in {n['secs']} s" if n["source"] == "gemini" else
           "Gemini unavailable — using the notice's structured copy")
    rel = f.get("relaxation", {})
    L = [f"### 📄 Official notification — {how}",
         f"- **{f.get('exam') or job['title']}** · Advt. `{f.get('advertisement_no') or '—'}` · "
         f"{f.get('total_vacancies') or job['posts']} posts · closes **{f.get('application_closes') or fmt(d(job['last_date']))}**",
         f"- Age **{f.get('age_min')}–{f.get('age_max')}** as on **{f.get('age_cutoff_date') or '—'}** · relaxation "
         f"OBC +{rel.get('OBC', 0)} · SC/ST +{rel.get('SC', 0)} · EWS +{rel.get('EWS', 0)}",
         f"- Qualification: **{(f.get('min_education') or '').capitalize() or job['eligibility']}**",
         f"- The application form asks for {len(f.get('form_fields', []))} details — Agent 2 can fill it for you."]
    cat = {"OBC": "OBC", "EWS": "EWS", "SC": "SC/ST", "ST": "SC/ST"}.get(p.get("category", ""), "")
    docs = [x for x in f.get("required_documents", []) if x.get("for") in ("all", cat)
            or (x.get("for") == "PwBD" and p.get("pwbd")) or x.get("for") == "conditional"]
    if docs:
        L += ["", "### 📑 Documents this notification asks you for"]
        L += [f"- {x['document']}" + (" _(only if names differ across documents)_" if x.get("for") == "conditional" else "")
              for x in docs]
    return L


DOC_WHY = {
    "class10": "proof of name and date of birth",
    "class12": "essential qualification",
    "diploma": "essential qualification",
    "degree": "essential qualification",
    "aadhaar": "photo ID — mask the first 8 digits",
    "obc_ncl": "needed for OBC age relaxation and reserved seats",
    "ews": "needed for EWS reserved seats",
    "sc_st": "needed for SC/ST relaxation, fee exemption and reserved seats",
    "pwbd": "needed for PwBD relaxation and reserved seats",
}
READ_FIELD_LABEL = {"name": "Name", "father_name": "Father", "dob": "DOB", "issue_date": "Issued",
                    "category": "Category", "valid_fy": "Valid FY"}


def required_docs(p: Dict[str, Any], job: Optional[Dict[str, Any]] = None) -> List[str]:
    """Standard document set for central recruitment, scoped to the job's qualification and the candidate."""
    req = ["class10", "aadhaar"]
    req += {2: ["class12"], 3: ["diploma"], 4: ["degree"], 5: ["degree"]}.get(job["min_edu"] if job else p.get("edu", 0), [])
    req += {"OBC": ["obc_ncl"], "EWS": ["ews"], "SC": ["sc_st"], "ST": ["sc_st"]}.get(p.get("category", ""), [])
    if p.get("pwbd"):
        req.append("pwbd")
    return req


def read_summary(fields: Dict[str, Any]) -> str:
    bits = [f"{READ_FIELD_LABEL[k]}: **{fields[k]}**" for k in READ_FIELD_LABEL if fields.get(k)]
    if fields.get("aadhaar_last4"):
        bits.append(f"Aadhaar ••••{fields['aadhaar_last4']}")
    if fields.get("central_format") is False:
        bits.append("⚠️ State format")
    return " · ".join(bits) or "no fields read"


def apply_read(p: Dict[str, Any], doc_type: str, f: Dict[str, Any]) -> None:
    """Copies what Gemini Vision read into the audit inputs (the same fields the typed form fills)."""
    docs = p.setdefault("docs", {})
    if doc_type == "class10":
        docs.update({k: v for k, v in (("marksheet_name", f.get("name")), ("marksheet_dob", f.get("dob"))) if v})
        for k in ("father_name", "mother_name"):
            if f.get(k) and not docs.get(k):
                docs[k] = f[k]
    elif doc_type == "aadhaar":
        docs.update({k: v for k, v in (("aadhaar_name", f.get("name")), ("aadhaar_dob", f.get("dob"))) if v})
    elif doc_type in ("obc_ncl", "ews", "sc_st"):
        docs["cert_type"] = {"obc_ncl": "OBC-NCL", "ews": "EWS"}.get(doc_type) or f.get("category") or "SC"
        if f.get("issue_date"):
            docs["cert_issue_date"] = f["issue_date"]
        if doc_type == "obc_ncl" and isinstance(f.get("central_format"), bool):
            docs["cert_central"] = "yes" if f["central_format"] else "no"


def build_checklist_card(p: Dict[str, Any], job: Optional[Dict[str, Any]], req: List[str]) -> dict:
    """Card Kind: custom — one row per required document with its status, plus actions."""
    files = p.get("upload", {}).get("files", {})
    rows = []
    for x in req:
        f = files.get(x)
        label, variant = (("Check photo", "warning") if f.get("quality") else ("Read ✓", "success")) if f else ("Pending", "info")
        rows.append({"children": [
            {"type": "group", "direction": "row", "gap": 8, "children": [
                {"type": "badge", "label": label, "variant": variant},
                {"type": "text", "value": DOC_TYPES[x], "style": "emphasis"}]},
            {"type": "text", "value": re.sub(r"\*\*", "", read_summary(f["fields"])) if f else DOC_WHY[x].capitalize(),
             "style": "muted"}]})
    have_core = bool(p.get("docs", {}).get("marksheet_name") and p.get("docs", {}).get("aadhaar_name"))
    buttons = []
    if have_core:
        buttons.append({"type": "button", "label": "Run audit", "primary": True,
                        "action": {"selection": {"action": "run_audit"}}})
    buttons.append({"type": "button", "label": "Type details instead", "action": {"selection": {"action": "doc_form"}}})
    if not have_core:
        buttons.append({"type": "button", "label": "🧪 Use sample documents",
                        "action": {"selection": {"action": "use_sample_docs"}}})
    if job:
        buttons.append({"type": "button", "label": "Back to post", "action": {"selection": {"action": "pick_job", "job_id": job["id"]}}})
    done = sum(1 for x in req if x in files)
    return {"root": {"type": "section", "title": f"Documents{' for ' + job['short'] if job else ''}",
                     "subtitle": f"{done}/{len(req)} read with Gemini Vision · upload with + in the chat box", "children": [
                         {"type": "list", "items": rows}, {"type": "divider"},
                         {"type": "group", "direction": "row", "gap": 8, "children": buttons}]}}


def build_checklist(p: Dict[str, Any], note: str = "") -> Dict[str, Any]:
    job = find_job_by_id(p.get("last_job") or "")
    req = required_docs(p, job)
    files = p.get("upload", {}).get("files", {})
    nxt = next((x for x in req if x not in files), None)
    L = [f"## 📑 Documents{' for ' + job['short'] if job else ''}"]
    if note:
        L += ["", note]
    L.append("")
    for x in req:
        f = files.get(x)
        L.append(f"- {('⚠️' if f.get('quality') else '✅') if f else '⬜'} **{DOC_TYPES[x]}** — "
                 + (read_summary(f["fields"]) + (f" · photo: {f['quality']}" if f.get("quality") else "") if f else DOC_WHY[x]))
    L.append("")
    if nxt:
        L.append(f"👉 **Upload next: {DOC_TYPES[nxt]}** — tap **+** in the chat box, attach a photo or PDF, and send.")
    else:
        L.append("✅ All documents read — tap **Run audit**.")
    L.append("_🔒 Use a masked Aadhaar. Each file is read once with Google Gemini; ApplyEase keeps only the "
             "extracted names and dates, and `reset` deletes them._")
    return pack("\n".join(L), "custom", build_checklist_card(p, job, req), p)


def ask_documents(p: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    docs = p.get("docs", {})
    have = [f"{label}: **{docs[k]}**" for k, label in (("marksheet_name", "Class 10 name"), ("aadhaar_name", "Aadhaar name"))
            if docs.get(k)]
    L = [prefix + "## 📑 Document check",
         "Name or DOB mismatches between Class 10 and Aadhaar are a common reason for rejection at document "
         "verification. Fill the card, or type it, e.g.:",
         "*`marksheet name Rajesh Kumar Sharma, aadhaar name Rajesh K Sharma, OBC certificate issued 15/01/2026`*"]
    if have:
        L += ["", "Got so far: " + " · ".join(have)]
    return pack("\n".join(L), "form", build_document_form(p), p)


SOURCE_LINE = {"local": "📐 Checked by ApplyEase's document rules (Class 10 = name/DOB reference · DoPT certificate validity)"}


def audit_outcome(p: Dict[str, Any], result: Dict[str, Any], source: str) -> Dict[str, Any]:
    docs = p.get("docs", {})
    name = docs.get("marksheet_name", "")
    icon, label = VERDICT_TEXT[result["verdict"]]
    src = SOURCE_LINE[source]
    if source == "agent2" and result.get("engine") == "rules+gemini":
        src += " · summary by Gemini"
    L = [f"## 📑 Document check — {icon} {label}",
         f"**{name}** · score **{result['compliance_score']}/100**",
         f"> {src}", "", result["summary"], "", "### Findings"]
    for c in result["checks"]:
        L.append(f"- {ICON.get({'ok': 'ok', 'minor': 'warn', 'major': 'warn', 'critical': 'block'}[c['severity']])} "
                 f"**{c['field']}** — {c['finding']}" + (f"\n  👉 {c['remedy']}" if c.get("remedy") else ""))
    if result["affidavit_needed"]:
        L += ["", "📜 A name affidavit is recommended — tap **View affidavit draft** or type `affidavit`."]
    L += ["", "---", data_footer()]
    card = build_audit_card(name, {**result, "source": source}, "rule engine")
    job = find_job_by_id(p.get("last_job") or "")
    if job and job.get("form_url"):
        card["root"]["children"][-1]["children"].insert(0, {"type": "button", "label": "✍️ Fill application (Agent 2)",
                                                            "primary": not result.get("affidavit_needed"),
                                                            "action": {"selection": {"action": "fill_form"}}})
    return pack("\n".join(L), "custom", card, p)


def start_audit(p: Dict[str, Any], force_form: bool = False) -> Dict[str, Any]:
    """Runs the deterministic document audit (instant) on the typed or Gemini-read details."""
    docs = p.get("docs", {})
    if force_form:
        return ask_documents(p)
    if not (docs.get("marksheet_name") and docs.get("aadhaar_name")):
        return build_checklist(p)
    if docs.get("marksheet_dob") and calculate_age_from_dob(docs["marksheet_dob"]) is not None:
        p["dob"], p["age"] = docs["marksheet_dob"], calculate_age_from_dob(docs["marksheet_dob"])  # Class 10 DOB is final
    req = doc_request(p)
    result = run_dossier_audit(req)
    result = {"verdict": result["verdict"], "compliance_score": result["compliance_score"], "summary": result["summary"],
              "checks": result["checks"], "actions": result["actions"], "affidavit_needed": result["affidavit_needed"],
              "affidavit_text": result["affidavit_text"], "engine": "rules", "source": "local"}
    p["last_audit"] = result
    return audit_outcome(p, result, "local")


def build_affidavit_view(p: Dict[str, Any]) -> Dict[str, Any]:
    res = p.get("last_audit")
    if not res:
        return ask_documents(p, "ℹ️ Run the document check first — the affidavit is drafted from its findings.\n\n")
    if not res.get("affidavit_text"):
        return pack("✅ Your names match, so no name affidavit is needed. Apply with the Class 10 name.",
                    "custom", build_audit_card(p.get("docs", {}).get("marksheet_name", ""), res, "rule engine"), p)
    L = ["## 📜 Name affidavit — draft",
         "Fields in [brackets] are left blank on purpose: fill them by hand. Nothing here was guessed.", "",
         "```text", res["affidavit_text"], "```"]
    return pack("\n".join(L), "review", build_affidavit_card(p.get("docs", {}), p.get("gender", "")), p)


def build_affidavit_done(p: Dict[str, Any]) -> Dict[str, Any]:
    L = ["## ✅ Getting the affidavit notarised"] + [f"{i}. {t}" for i, t in enumerate(AFFIDAVIT_STEPS, 1)]
    L += ["", "Next: type `show jobs` to go back to your posts."]
    return pack("\n".join(L), "custom", build_done_card(), p, terminal=True)


def build_ladder(p: Dict[str, Any]) -> Dict[str, Any]:
    if "age" not in p or "edu" not in p:
        return welcome("ℹ️ Tell me your date of birth and qualification first.\n\n")
    t2 = tier2(p, {j["id"] for j in tier1(p)})
    L = ["## 🪜 Career ladder (Tier 2)"]
    for j in t2:
        L.append(f"- **{j['short']}** ({j['pay']}) — {j['_path']} → eligible at age {j['_elig_age']} "
                 f"(~{j['_elig_year']}), ~{j['_window']} attempt(s)")
    if not t2:
        L.append("_You already meet the qualification bar for every higher post tracked._")
    return pack("\n".join(L), "custom", build_ladder_card(p, t2), p)


CARD_ACTIONS = ("submit_profile", "submit_documents", "pick_job", "roadmap", "audit_dossier", "doc_form", "get_affidavit",
                "fill_form", "form_answers", "submit_form", "use_sample_docs",
                "finalise_affidavit", "show_carousel", "show_ladder", "show_form", "doc_checklist", "run_audit")
FORM_FIELDS = ("dob", "edu", "percentage", "gap_years", "job_sector", "category", "gender", "job_id") + DOC_KEYS


# ───────────────────────────── Application form (Agent 2 · Form Filler) ─────────────────────────────
FORM_LINK = re.compile(r"https?://(?:docs\.google\.com/forms/[^\s)>\]]+|forms\.gle/[^\s)>\]]+)")
QUAL_LEVEL = {1: "10th", 2: "12th", 3: "Diploma", 4: "Graduation", 5: "Post Graduation"}


def extract_applicant(text: str) -> Dict[str, str]:
    """'Label: value' lines of an applicant-details message (e.g. 'Mobile Number: 9876543210')."""
    out = {}
    for label, value in re.findall(r"^\s*([A-Za-z][A-Za-z0-9 '’()/.&-]{1,45}?)\s*[:=]\s*(.+?)\s*$", text, re.M):
        if not FORM_LINK.search(value):
            out[_clean_label(label)] = value.strip()
    return out


def _clean_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.replace("’", "'")).strip()


def candidate_for_form(p: Dict[str, Any]) -> Dict[str, Any]:
    """Everything known about the candidate, documents first (Class 10 = legal name/DOB). Full Aadhaar is never
    stored by Agent 1 — the form asks the user for it directly."""
    docs = p.get("docs", {})
    c = {
        "full_name": docs.get("marksheet_name") or p.get("name"), "father_name": docs.get("father_name"),
        "mother_name": docs.get("mother_name"), "dob": docs.get("marksheet_dob") or p.get("dob"),
        "gender": GENDER.get(p.get("gender", ""), ""), "category": p.get("category"),
        "highest_qualification_level": QUAL_LEVEL.get(p.get("edu", 0), ""), "nationality": "India",
        "pwbd": "Yes" if p.get("pwbd") else ("No" if "pwbd" in p else None),
        "aadhaar_last4": next((f["fields"].get("aadhaar_last4") for f in p.get("upload", {}).get("files", {}).values()
                               if f.get("fields", {}).get("aadhaar_last4")), None),
    }
    c = {k: v for k, v in c.items() if v not in (None, "")}
    c.update(p.get("applicant", {}))  # the user's own 'Label: value' details win
    return c


def start_fill(p: Dict[str, Any], submit: bool = False) -> Dict[str, Any]:
    job = find_job_by_id(p.get("last_job") or "")
    url = p.get("form_url") or (job or {}).get("form_url")
    if not url:
        return pack("✍️ Open a post that has an online form (e.g. **ADRB CGLE (Demo)**), or paste a Google Form link "
                    "and I'll have Agent 2 fill it.", "carousel", build_jobs_carousel(p, tier1(p)) if "age" in p and "edu" in p
                    else build_jobs_carousel(p, []), p)
    if not AGENT_2_ADDRESS:
        return pack("⚠️ Form filling is switched off (AGENT_2_ADDRESS is empty).", "custom",
                    note_card("Form filling off", "Set AGENT_2_ADDRESS to the Form Filler agent."), p)
    out = pack("", "custom", note_card("Form", ""), p)  # placeholder — the reply is built from Agent 2's answer
    out["form_fill"] = {"form_url": url, "candidate_json": json.dumps(candidate_for_form(p), ensure_ascii=False),
                        "answers_json": json.dumps(p.get("form_answers", {}), ensure_ascii=False), "submit": submit}
    return out


# Sample (dummy) candidate — lets anyone try every step without real data. Values are fictional.
SAMPLE_PROFILE = {"dob": "14/05/1998", "edu": 4, "category": "OBC", "gender": "M"}
SAMPLE_DOCS = {"marksheet_name": "Aarav Sharma", "aadhaar_name": "Aarav K Sharma", "father_name": "Rajesh Sharma",
               "mother_name": "Sunita Sharma", "marksheet_dob": "14/05/1998", "aadhaar_dob": "14/05/1998",
               "cert_type": "OBC-NCL", "cert_issue_date": "10/06/2026"}
SAMPLE_APPLICANT = {"Mobile Number": "9876543210", "Email Address": "aarav.sharma.dummy@example.com",
                    "Permanent Address": "Flat 402, Sunshine Apartments, MG Road, Delhi 110001",
                    "Present Address": "Flat 402, Sunshine Apartments, MG Road, Delhi 110001",
                    "10th Board": "CBSE", "10th Roll Number": "12345678", "12th Board": "CBSE",
                    "12th Roll Number": "87654321", "Graduation Degree": "B.Tech (Computer Science)",
                    "Graduation Year": "2020", "Graduation Percentage": "78.5", "Nationality": "India"}
SAMPLE_ANSWERS = [  # (question pattern, dummy answer) for questions Agent 2 couldn't fill
    (r"aadhaa?r.*(last ?4|last four)", "9012"), (r"aadhaa?r", "123456789012"),
    (r"roll.*(10|tenth|matric|secondary)|(10|tenth|matric).*roll", "12345678"),
    (r"roll.*(12|twelfth|intermediate|higher)|(12|twelfth|intermediate|higher).*roll", "87654321"),
    (r"mobile|phone|contact", "9876543210"), (r"e-?mail", "aarav.sharma.dummy@example.com"),
    (r"address", "Flat 402, Sunshine Apartments, MG Road, Delhi 110001"), (r"father", "Rajesh Sharma"),
    (r"mother", "Sunita Sharma"), (r"date of birth|\bdob\b|birth", "14/05/1998"), (r"board", "CBSE"),
    (r"gender|\bsex\b", "Male"), (r"categor|caste", "OBC"), (r"nationality", "India"),
    (r"percent|marks|cgpa", "78.5"), (r"year", "2020"), (r"degree|course|qualification", "Graduation"),
    (r"college|university|institut", "Delhi Technical University"), (r"state|domicile", "Delhi"),
    (r"pin ?code", "110001"), (r"city|district", "New Delhi"), (r"name", "Aarav Sharma"),
]


def is_checked(v: Any) -> bool:
    return v is True or (isinstance(v, list) and bool(v)) or str(v).strip().lower() in ("true", "on", "yes", "1", "checked")


def sample_answer(g: Dict[str, Any]) -> str:
    title = g.get("title", "").lower()
    val = next((a for rx, a in SAMPLE_ANSWERS if re.search(rx, title)), "Sample answer")
    opts = g.get("options") or []
    if opts:
        hit = next((o for o in opts if o.lower() == val.lower()), None)
        return hit or next((o for o in opts if val.lower() in o.lower() or o.lower() in val.lower()), opts[0])
    return val


def load_sample(p: Dict[str, Any], docs: bool = True) -> List[str]:
    """Fills only what is still empty; returns the labels it filled."""
    used = []
    for k, v in SAMPLE_PROFILE.items():
        if k not in p:
            p[k] = v
            used.append(k)
    if "age" not in p or "dob" in used:
        p["age"] = calculate_age_from_dob(p["dob"])
    if docs:
        d = p.setdefault("docs", {})
        for k, v in SAMPLE_DOCS.items():
            if not d.get(k):
                d[k] = v
                used.append(k)
        p["applicant"] = {**SAMPLE_APPLICANT, **p.get("applicant", {})}
    return used


SAMPLE_NOTE = "🧪 _Using **sample (dummy) data** for: {}. Type `reset` to start over with your own._\n\n"


def note_card(title: str, text: str) -> dict:
    return {"root": {"type": "section", "title": title, "children": [{"type": "text", "value": text}]}}


def _fmt_value(v: Any) -> str:
    return ", ".join(v) if isinstance(v, list) else (v.get("other", "") if isinstance(v, dict) else str(v))


def _masked(title: str, v: str) -> str:
    return "•••• •••• " + v[-4:] if re.search(r"aadhaa?r", title, re.I) and len(re.sub(r"\D", "", v)) >= 12 else v


def build_missing_form_card(title: str, fields: List[Dict[str, Any]]) -> dict:
    """Card Kind: form — generated from the questions Agent 2 could not answer (same types and options as the form)."""
    out = []
    for f in fields:
        label = f["title"][:90] + (f" — {f['reason']}" if f["status"] == "invalid" and f.get("reason") else "")
        name = "a_" + f["entry_id"].split(".", 1)[1]
        if f.get("options") and f["type"] in ("radio", "dropdown", "scale"):
            out.append({"name": name, "kind": "select", "label": label,
                        "options": [{"value": o, "label": o} for o in f["options"]]})
        else:
            hint = " (DD/MM/YYYY)" if f["type"] == "date" else (" (comma-separated)" if f["type"] == "checkbox" else "")
            kind = "email" if re.search(r"e-?mail", f["title"], re.I) else "text"
            out.append({"name": name, "kind": kind, "label": label + hint})
    return {"title": f"{title[:60]} — details Agent 2 still needs", "fields": out[:20] + [{"name": "use_sample", "kind": "checkbox", "label": "🧪 Fill anything I leave empty with sample (dummy) data"}],
            "submit_cta": {"label": "Send to Agent 2", "selection": {"action": "form_answers"}}}


def _safe_prefill(url: str, fields: List[Dict[str, Any]]) -> str:
    """The pre-filled link without Aadhaar answers — chat history must never carry the full number."""
    hidden = {f["entry_id"] for f in fields if re.search(r"aadhaa?r", f["title"], re.I)}
    if not url or not hidden or "?" not in url:
        return url
    base, query = url.split("?", 1)
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True) if k not in hidden]
    return base + "?" + urllib.parse.urlencode(kept)


def fill_outcome(p: Dict[str, Any], r: Dict[str, Any]) -> Dict[str, Any]:
    r = {**r, "prefill_url": _safe_prefill(r.get("prefill_url", ""), r["fields"])}
    fields = r["fields"]
    done = [f for f in fields if f["status"] == "filled"]
    gaps = [f for f in fields if f["status"] in ("missing", "invalid")]
    head = f"🤖 **Agent 2 · Form Filler** read **{r['form_title']}** — {len(done)}/{len(fields)} questions answered."
    rows = "\n".join(f"- ✅ **{f['title']}**: {_masked(f['title'], _fmt_value(f['value']))}" for f in done)
    if r["submitted"]:
        p.pop("form_answers", None)  # answers (incl. full Aadhaar) are not kept once submitted
        card = {"root": {"type": "section", "title": "Application submitted", "subtitle": r["form_title"], "children": [
            {"type": "group", "direction": "row", "gap": 8, "children": [
                {"type": "badge", "label": "Recorded by Google", "variant": "success"},
                {"type": "badge", "label": f"{len(done)}/{len(fields)} answered", "variant": "info"},
                {"type": "badge", "label": "Agent 2 · Form Filler", "variant": "success"}]},
            {"type": "list", "items": [{"children": [{"type": "text", "value": f["title"], "style": "emphasis"},
                                                     {"type": "text", "value": _masked(f["title"], _fmt_value(f["value"])),
                                                      "style": "muted"}]} for f in done[:20]]}]}}
        md = (f"## ✅ Application submitted\n{head}\n\n{rows}\n\n"
              "Google recorded the response. Keep your documents ready for verification — tap **Check my documents** "
              "if you haven't yet.")
        return pack(md, "custom", card, p, terminal=True)
    if not r["fields"]:
        return pack(f"⚠️ **Agent 2 couldn't fill this form:** {r['message']}"
                    + (f"\n\n[Open the form]({r['prefill_url']})" if r.get("prefill_url") else ""),
                    "custom", note_card("Form not filled", r["message"][:300]), p)
    if gaps:
        p["fill_gaps"] = [{k: g[k] for k in ("entry_id", "title", "type", "options")} for g in gaps]
        md = (f"## ✍️ Almost there\n{head}\n\n{rows}\n\n**Agent 2 needs {len(gaps)} more detail(s)** — fill the card below:\n"
              + "\n".join(f"- ❗ **{f['title']}**" + (f" — {f['reason']}" if f["status"] == "invalid" else "") for f in gaps)
              + f"\n\n_Or [open the pre-filled form]({r['prefill_url']}) and finish it yourself._")
        return pack(md, "form", build_missing_form_card(r["form_title"], gaps), p)
    md = (f"## 📝 Review your application\n{head}\n\n{rows}\n\nNothing is missing. Check the values, then tap "
          f"**Submit application** — Agent 2 will submit it to Google.\n\n_[Preview the pre-filled form]({r['prefill_url']})_")
    card = {"title": f"Submit: {r['form_title'][:60]}",
            "summary_rows": [{"label": f["title"][:40], "value": _masked(f["title"], _fmt_value(f["value"]))[:80]} for f in done[:20]],
            "approve_cta": {"label": "Submit application (Agent 2)", "primary": True, "selection": {"action": "submit_form"}},
            "reject_cta": {"label": "Not now", "selection": {"action": "show_carousel"}}}
    return pack(md, "review", card, p)


def parse_card_action(text: str) -> Optional[Dict[str, Any]]:
    """Card selections arrive as JSON (direct @mention) or as prose that names every identifier (planner-routed)."""
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            data = json.loads(t)
            if isinstance(data, dict):
                if isinstance(data.get("selection"), dict):
                    return {**data, **data["selection"]}
                return data
        except Exception:
            pass
    m = re.search(r"\b(" + "|".join(CARD_ACTIONS) + r")\b", t)
    if not m:
        return None
    out: Dict[str, Any] = {"action": m.group(1)}
    names = "|".join(FORM_FIELDS)
    for f in FORM_FIELDS:
        v = re.search(rf"\b{f}\b[\"']?\s*(?:is|=|:|of|as|to)?\s*[\"']?(.+?)[\"']?\s*(?=,|;|\.(?:\s|$)|\band\b|\b(?:{names})\b|$)", t)
        if v and v.group(1).strip() and v.group(1).strip().lower() not in CARD_ACTIONS:
            out[f] = v.group(1).strip()
    if "job_id" not in out:
        ids = re.search(r"\b(" + "|".join(re.escape(j["id"]) for j in fetch_vacancies()) + r")\b", t)
        if ids:
            out["job_id"] = ids.group(1)
    return out


def answer_conversational_query(text: str, profile: Dict[str, Any], relevant_jobs: List[Dict[str, Any]]) -> Optional[str]:
    """Answers free-form questions with Gemini Flash. None when Gemini is unavailable — the caller then replies
    with the rule engine's real content instead of a canned line."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        job_summaries = "\n".join(
            f"- {j['short']} ({j['title']}): Min Edu: {EDU.get(j['min_edu'])}, Age: {j['min_age']}-{j['max_age']}, Pay: {j['pay']}, Last Date: {j['last_date']}, Stream: {j['stream']}, Relax: {j['relax']}"
            for j in relevant_jobs[:8]
        )
        prompt = f"""You are ApplyEase, an expert AI Govt Career & Compliance Assistant on Fetch.ai / ASI:One.
Candidate Profile:
- Age: {profile.get('age', 'Not specified')}
- Education: {EDU.get(profile.get('edu', 2), '12th Pass')}
- Category: {profile.get('category', 'UR')}
- Gender: {profile.get('gender', 'Not specified')}

Relevant Government Vacancies in database:
{job_summaries}

User Question: "{text}"

Answer directly, accurately, and concisely in friendly markdown (with Hinglish / English touch if user asks in Hindi/Hinglish).
Include exact eligibility criteria, age limits, relaxations, or application instructions as relevant. Keep it under 150 words.
"""
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024}
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return answer
    except Exception:
        return None


def evaluate_query(text: str, stored: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Returns (outcome{markdown, metadata}, updated_profile)."""
    # 0. Strip leading @mentions (e.g. '@applyeasee HII' -> 'HII')
    clean_text = re.sub(r"^@\w+\s*", "", text).strip()
    q = clean_text.lower().strip()

    # 1. Reset / Clear
    if re.fullmatch(r"(reset|clear|start over|new profile|delete my data)\W*", q):
        return welcome("🔄 Profile cleared.\n\n"), {}

    # 1b. Sample candidate for testing
    if re.fullmatch(r"(try )?(demo|sample)( (data|profile|candidate|mode))?|use sample( data)?\W*", q):
        prof = {} if q.startswith(("demo", "try demo")) else dict(stored)
        load_sample(prof)
        out = build_assessment(prof, None)
        out["markdown"] = (SAMPLE_NOTE.format("Aarav Sharma — DOB 14/05/1998, B.Tech, OBC, male; his Class 10 "
                                              "and Aadhaar names; contact details")
                           + out["markdown"] + "\n\n👉 Next: open **ADRB CGLE (Demo)** → **Check my documents** → "
                           "**Fill application form (Agent 2)**.")
        return out, prof

    # 2. Greeting check (matches 'hi', 'hii', 'hiii', 'hello', 'hey', 'start', etc.)
    is_greeting = bool(re.search(r"^(hi+|hello+|hey+|namaste|start|hola)\b", q))
    if is_greeting:
        return welcome("👋 Hello! You can set up or update your profile in the card below:\n\n"), stored

    profile = dict(stored)

    # 3. Check if this is an interactive card action / form submission
    card_action = parse_card_action(clean_text) or parse_card_action(text)
    if card_action:
        # Answers for the questions Agent 2 couldn't fill → preview again
        if card_action.get("action") == "form_answers" or any(re.fullmatch(r"a_\d+", str(k)) for k in card_action):
            answers = dict(profile.get("form_answers", {}))
            for k, v in card_action.items():
                if re.fullmatch(r"a_\d+", str(k)) and str(v).strip():
                    answers["entry." + k[2:]] = str(v).strip()[:300]
            if is_checked(card_action.get("use_sample")):
                for g in profile.get("fill_gaps", []):
                    if not answers.get(g["entry_id"]):
                        answers[g["entry_id"]] = sample_answer(g)
            profile["form_answers"] = answers
            return start_fill(profile), profile
        if card_action.get("action") == "use_sample_docs":
            used = load_sample(profile)
            out = start_audit(profile)
            out["markdown"] = SAMPLE_NOTE.format("the Class 10 and Aadhaar details") + out["markdown"]
            return out, profile
        if card_action.get("action") in ("fill_form", "submit_form"):
            if card_action.get("job_id"):
                profile["last_job"], profile["form_url"] = card_action["job_id"], ""
            return start_fill(profile, submit=card_action["action"] == "submit_form"), profile

        # Typed document details
        if card_action.get("action") == "submit_documents" or "marksheet_name" in card_action:
            docs = dict(profile.get("docs", {}))
            for k in DOC_KEYS:
                v = card_action.get(k)
                if v is not None and str(v).strip():
                    docs[k] = re.sub(r"\s+", " ", str(v)).strip()[:80]
            profile["docs"] = docs
            note = ""
            if is_checked(card_action.get("use_sample")):
                filled = [k for k, v in SAMPLE_DOCS.items() if not docs.get(k)]
                for k in filled:
                    docs[k] = SAMPLE_DOCS[k]
                note = SAMPLE_NOTE.format(", ".join(k.replace("_", " ") for k in filled)) if filled else ""
            out = start_audit(profile)
            out["markdown"] = note + out["markdown"]
            return out, profile

        # Form submission: {action: 'submit_profile', dob: '2005-08-15', edu: 2, ...}
        if card_action.get("action") == "submit_profile" or "dob" in card_action or "age" in card_action:
            for k in ("age", "dob", "edu", "category", "gender", "percentage", "gap_years", "job_sector"):
                if k in card_action and card_action[k] is not None and str(card_action[k]).strip() != "":
                    v = card_action[k]
                    if k in ("edu", "gap_years"):
                        try:
                            profile[k] = int(v)
                        except Exception:
                            profile[k] = v
                    elif k == "percentage":
                        try:
                            profile[k] = float(v)
                        except Exception:
                            pass
                    elif k in ("category", "gender", "job_sector"):
                        profile[k] = str(v).upper()
                    elif k == "dob":
                        profile["dob"] = str(v).strip()
                        calced_age = calculate_age_from_dob(profile["dob"])
                        if calced_age is not None:
                            profile["age"] = calced_age
                    elif k == "age":
                        try:
                            profile["age"] = int(v)
                        except Exception:
                            pass
            note = ""
            if is_checked(card_action.get("use_sample")):
                used = load_sample(profile, docs=False)
                note = SAMPLE_NOTE.format(", ".join(used)) if used else ""
            if "dob" in profile and "age" not in profile:
                profile["age"] = calculate_age_from_dob(profile["dob"])
            out = build_assessment(profile, None)
            out["markdown"] = note + out["markdown"]
            return out, profile

        action = card_action.get("action")
        job_id = card_action.get("job_id")
        job = find_job_by_id(job_id) if job_id else None

        if job:
            profile["last_job"] = job["id"]
        if action == "pick_job" and job:
            return build_post_detail(profile, job), profile
        elif action == "roadmap" and job:
            return build_roadmap(profile, job), profile
        elif action in ("audit_dossier", "doc_checklist"):
            return build_checklist(profile), profile
        elif action == "doc_form":
            return start_audit(profile, force_form=True), profile
        elif action == "run_audit":
            return start_audit(profile), profile
        elif action == "get_affidavit":
            return build_affidavit_view(profile), profile
        elif action == "finalise_affidavit":
            return build_affidavit_done(profile), profile
        elif action == "show_ladder":
            return build_ladder(profile), profile
        elif action == "show_carousel":
            return build_assessment(profile, None), profile
        elif action == "show_form":
            return welcome(), profile

    # 3b. Google Form link and/or 'Label: value' applicant details → Agent 2 fills it
    link = FORM_LINK.search(clean_text)
    applicant = extract_applicant(clean_text)
    if link or len(applicant) >= 3:
        if link:
            profile["form_url"] = link.group(0)
        if applicant:
            profile["applicant"] = {**profile.get("applicant", {}), **applicant}
        if link or profile.get("form_url") or (find_job_by_id(profile.get("last_job") or "") or {}).get("form_url"):
            return start_fill(profile), profile
    if re.search(r"\b(fill|submit)\b.*\b(form|application)\b|\bapply (for me|now)\b", q):
        return start_fill(profile, submit=bool(re.search(r"\bsubmit\b", q))), profile

    # 4. Extract text updates
    extracted = extract_profile(clean_text)
    profile.update(extracted)
    if "dob" in profile and "age" not in profile:
        profile["age"] = calculate_age_from_dob(profile["dob"])
    mentioned = find_mentioned_job(clean_text)
    complete = ("age" in profile or "dob" in profile) and "edu" in profile and profile.get("age") is not None

    # 5. Check explicit intents
    if AUDIT_INTENT.search(q):
        docs = extract_docs(clean_text)
        if mentioned:
            profile["last_job"] = mentioned["id"]
        if docs:
            profile["docs"] = {**profile.get("docs", {}), **docs}
        elif re.search(r"\baffidavit\b", q) and profile.get("last_audit"):
            return build_affidavit_view(profile), profile
        else:
            return build_checklist(profile), profile
        return start_audit(profile), profile
    if re.search(r"\b(career ladder|ladder|tier ?2|future posts?)\b", q):
        return build_ladder(profile), profile
    if mentioned and ROADMAP_INTENT.search(q):
        return build_roadmap(profile, mentioned), profile
    if mentioned and (DETAIL_INTENT.search(q) or len(q.split()) <= 3):
        return build_post_detail(profile, mentioned), profile

    # 6. Profile incomplete → form card
    if not complete:
        missing = []
        if "age" not in profile and "dob" not in profile:
            missing.append("Date of Birth (DOB)")
        if "edu" not in profile:
            missing.append("qualification")
        need = " and ".join(missing) if missing else "your profile details"
        form_card = build_profile_form()
        return pack(f"📝 Please complete {need} in the form card below or type it directly:\n\n",
                    "form", form_card, profile), profile

    # 7. New profile facts, or "which jobs can I apply for?" → the full 3-tier assessment
    if not mentioned and (extracted or JOB_LIST_INTENT.search(q)):
        return build_assessment(profile, None), profile

    # 8. Any other question → Gemini answer grounded on the candidate's posts, with the matching card attached.
    #    Without Gemini (no key, quota, outage) the rule engine's real content is returned instead.
    t1 = tier1(profile)
    ans = answer_conversational_query(clean_text, profile, t1)
    if mentioned:
        if ans:
            return pack(f"{ans}\n\n---\n*Attached post details:*", "detail", build_job_detail_card(profile, mentioned), profile), profile
        return build_post_detail(profile, mentioned), profile
    if ans:
        return pack(f"{ans}\n\n---\n*Your matching vacancies:*", "carousel", build_jobs_carousel(profile, t1), profile), profile
    return build_assessment(profile, None), profile


# ───────────────────────────── ACP wiring ─────────────────────────────
# Every reply is sent in the same turn, except form filling: that request goes to Agent 2 · Form Filler and the
# single reply is sent when Agent 2 answers (or after FILL_TIMEOUT_S). ASI:One shows one reply per message.
chat_proto = Protocol(spec=chat_protocol_spec)
form_client = Protocol(name="FormFillClientProtocol", version="2.0.0")
FILL_KEY = "a2a:fills"  # {request_id: {"user": address, "ts": epoch, "submit": bool}}


def _resource_info(item: Any) -> Dict[str, Any]:
    res = item.resource[0] if isinstance(item.resource, list) else item.resource
    meta = dict(getattr(res, "metadata", {}) or {})
    return {"uri": str(getattr(res, "uri", "")), "meta": meta,  # ASI:One puts the original filename in "description"
            "name": meta.get("name") or meta.get("file_name") or meta.get("filename") or meta.get("description") or "upload"}


async def send_chat(ctx: Context, to: str, outcome: Dict[str, Any]) -> None:
    """Plain ctx.send only: Agentverse's hosted runtime has no send_raw and silently drops messages sent through it."""
    await ctx.send(to, ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[
            TextContent(type="text", text=outcome["markdown"]),
            MetadataContent(type="metadata", metadata=outcome["metadata"]),
        ],
    ))


def _load_profile(ctx: Context, user: str) -> Dict[str, Any]:
    try:
        return ctx.storage.get(f"profile:{user}") or {}
    except Exception:
        return {}


def _save_profile(ctx: Context, user: str, profile: Dict[str, Any]) -> None:
    try:
        ctx.storage.set(f"profile:{user}", profile)
    except Exception as e:
        ctx.logger.warning(f"storage unavailable: {e}")


@chat_proto.on_message(ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))

    files = [c for c in msg.content if ResourceContent is not None and isinstance(c, ResourceContent)]
    if files:
        await handle_uploads(ctx, sender, files)
        return

    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        if any(isinstance(c, StartSessionContent) for c in msg.content):
            text = "hello"
        else:
            return

    try:  # warm the live-data cache off the event loop (no-op while fresh; falls back internally)
        await asyncio.to_thread(fetch_vacancies)
    except Exception as e:
        ctx.logger.warning(f"live vacancy refresh failed: {e}")

    stored = _load_profile(ctx, sender)
    try:  # Gemini calls are blocking HTTP, so the engine runs off the event loop
        outcome, profile = await asyncio.to_thread(evaluate_query, text, stored)
    except Exception as e:  # never leave the user without a reply
        ctx.logger.exception(f"evaluation failed: {e}")
        outcome, profile = pack("⚠️ Sorry, I couldn't process that. Try typing *'reset'* or *'DOB 15/08/2005, 12th pass'*.",
                                "form", build_profile_form(), stored), stored
    fill = outcome.pop("form_fill", None)
    _save_profile(ctx, sender, profile)
    ctx.logger.info(f"{sender[:16]}… q='{text[:80]}' card={outcome['metadata'].get('card_kind')} fill={bool(fill)}")
    if fill:
        await dispatch_fill(ctx, sender, fill)
        return
    await send_chat(ctx, sender, outcome)


async def dispatch_fill(ctx: Context, user: str, fill: Dict[str, Any]) -> None:
    rid = str(uuid4())
    await ctx.send(AGENT_2_ADDRESS, FormFillRequest(request_id=rid, candidate_id=user, **fill))
    fills = ctx.storage.get(FILL_KEY) or {}
    fills[rid] = {"user": user, "ts": time.time(), "submit": fill["submit"]}
    ctx.storage.set(FILL_KEY, fills)
    ctx.logger.info(f"A2A → Agent 2 (Form Filler): {'submit' if fill['submit'] else 'preview'} {rid[:8]} for {user[:16]}…")


async def handle_uploads(ctx: Context, sender: str, files: List[Any]) -> None:
    """Reads each new upload with Gemini Vision right here and replies with the updated checklist (same turn)."""
    profile = _load_profile(ctx, sender)
    job = find_job_by_id(profile.get("last_job") or "")
    have = profile.get("upload", {}).get("files", {})
    queue = [x for x in required_docs(profile, job) if x not in have]
    # ASI:One may resend earlier attachments of the chat with a new message: skip files already read, newest first.
    seen = profile.setdefault("upload", {}).setdefault("seen", [])
    fresh = [f for f in reversed(files) if str(f.resource_id) not in seen][:3]
    if not fresh:
        await send_chat(ctx, sender, build_checklist(profile, "ℹ️ That file was already read — attach a new photo or "
                                                               "PDF with **+**."))
        return
    notes = []
    for i, f in enumerate(fresh):
        info = _resource_info(f)
        seen.append(str(f.resource_id))
        expected = queue[i] if i < len(queue) else ""
        if not FILE_HOSTS.match(info["uri"]):
            notes.append(f"⚠️ I couldn't open `{info['name']}` — send it again as a JPG, PNG or PDF.")
            continue
        t0 = time.time()
        try:
            r = await asyncio.to_thread(read_document, info["uri"], expected)
        except GeminiQuotaError as e:
            ctx.logger.warning(str(e)[:600])
            r = {"ok": False, "doc_type": "", "fields": {}, "quality": "",
                 "detail": "Gemini is busy or over its limit — wait a minute and send it again"}
        except Exception as e:
            r = {"ok": False, "doc_type": "", "fields": {}, "quality": "", "detail": f"{type(e).__name__}: {str(e)[:160]}"}
        secs = time.time() - t0
        ctx.logger.info(f"doc read {info['name'][:30]}: {r['doc_type'] or '-'} ok={r['ok']} fields={sorted(r['fields'])} "
                        f"in {secs:.1f}s model={r.get('model') or '-'} size={r.get('bytes', '-')} ({r['detail']})")
        if r["ok"]:
            profile["upload"].setdefault("files", {})[r["doc_type"]] = {"fields": r["fields"], "quality": r["quality"],
                                                                          "file": info["name"][:60]}
            apply_read(profile, r["doc_type"], r["fields"])
            which = f"your **{DOC_TYPES[r['doc_type']]}**"
            if expected and r["doc_type"] != expected:
                which += f" (I'd asked for the {DOC_TYPES[expected]} — saved it as {DOC_TYPES[r['doc_type']]})"
            notes.append(f"📄 Read {which} with Gemini Vision in {secs:.0f} s: {read_summary(r['fields'])}"
                         + (f"\n⚠️ Photo quality: {r['quality']} — re-upload a clearer one if anything above is wrong."
                            if r["quality"] else ""))
        else:
            notes.append(f"⚠️ **Couldn't read `{info['name']}`** — {r['detail']}. Try a flat, well-lit photo or a PDF, "
                         "or tap **Type details instead**.")
    del seen[:-30]
    _save_profile(ctx, sender, profile)
    await send_chat(ctx, sender, build_checklist(profile, "\n\n".join(notes)
                                                 + "\n\nIf a value is wrong, tap **Type details instead** to correct it."))


@form_client.on_message(model=FormFillResponse)
async def handle_fill_response(ctx: Context, sender: str, msg: FormFillResponse):
    fills = ctx.storage.get(FILL_KEY) or {}
    job = fills.pop(msg.request_id, None)
    if not job:
        ctx.logger.info(f"late/unknown Form Filler reply {msg.request_id[:8]} ignored")
        return
    ctx.storage.set(FILL_KEY, fills)
    try:
        fields = json.loads(msg.fields_json or "[]")
    except ValueError:
        fields = []
    profile = _load_profile(ctx, job["user"])
    outcome = fill_outcome(profile, {"ok": msg.ok, "submitted": msg.submitted, "form_title": msg.form_title,
                                     "fields": fields, "prefill_url": msg.prefill_url, "message": msg.message})
    _save_profile(ctx, job["user"], profile)
    ctx.logger.info(f"A2A ← Agent 2: {msg.request_id[:8]} ok={msg.ok} submitted={msg.submitted} "
                    f"in {time.time() - job['ts']:.1f}s — {msg.message}")
    await send_chat(ctx, job["user"], outcome)


@agent.on_interval(period=3.0)
async def expire_fills(ctx: Context):
    fills = ctx.storage.get(FILL_KEY) or {}
    late = [rid for rid, j in fills.items() if time.time() - j["ts"] > FILL_TIMEOUT_S]
    for rid in late:
        job = fills.pop(rid)
        ctx.storage.set(FILL_KEY, fills)
        ctx.logger.warning(f"Form Filler request {rid[:8]} timed out")
        await send_chat(ctx, job["user"], pack(
            f"⚠️ **Agent 2 · Form Filler** didn't answer within {FILL_TIMEOUT_S} s. Tap **✍️ Fill application form** "
            "to try again.", "custom", note_card("Form Filler timed out", "Try again in a minute."), {}))


@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.debug(f"ack from {sender[:16]}… for {msg.acknowledged_msg_id}")


agent.include(chat_proto, publish_manifest=True)
agent.include(form_client)
