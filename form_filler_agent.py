

import asyncio
import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec

agent = Agent()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",") if m.strip()]
GEMINI_TIMEOUT_S = 30
HTTP_TIMEOUT_S = 15
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


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


# ───────────────────────────── 1. Reading the form ─────────────────────────────
QTYPE = {0: "text", 1: "paragraph", 2: "radio", 3: "dropdown", 4: "checkbox", 5: "scale", 7: "grid",
         9: "date", 10: "time", 13: "file"}
SKIP_TYPES = {6, 11, 12}  # title/description blocks, images, videos
PAGE_BREAK = 8


def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("\xa0", " ")).strip()


def _get(url: str, timeout: float = HTTP_TIMEOUT_S) -> Tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace"), resp.geturl()


def form_urls(url: str) -> Tuple[str, str]:
    """(viewform URL, formResponse URL) for /forms/d/e/<id>/… links; forms.gle short links are resolved."""
    url = url.strip()
    if "forms.gle" in url:
        _, url = _get(url)
    m = re.search(r"/forms/d/(e/)?([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError("not a Google Form link")
    base = f"https://docs.google.com/forms/d/{m.group(1) or ''}{m.group(2)}"
    return base + "/viewform", base + "/formResponse"


def read_form(url: str) -> Dict[str, Any]:
    view_url, submit_url = form_urls(url)
    html, final = _get(view_url)
    if "accounts.google.com" in final or "ServiceLogin" in final:
        return {"title": "Google Form", "questions": [], "needs_login": True, "view_url": view_url,
                "submit_url": submit_url, "pages": 1, "fbzx": ""}
    m = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", html, re.S)
    if not m:
        raise ValueError("couldn't read this form (is it public and accepting responses?)")
    data = json.loads(m.group(1))
    info = data[1]
    questions: List[Dict[str, Any]] = []
    pages, needs_login = 1, False
    for item in info[1] or []:
        qtype = item[3] if len(item) > 3 else 0
        if qtype == PAGE_BREAK:
            pages += 1
            continue
        if qtype in SKIP_TYPES or len(item) < 5 or not item[4]:
            continue
        if qtype == 13:
            needs_login = True  # file-upload questions require a signed-in respondent
        title, desc = _clean(item[1]), _clean(item[2])
        for slot in item[4]:
            opts = [{"label": _clean(o[0]), "other": bool(len(o) > 4 and o[4])} for o in (slot[1] or []) if o]
            row = _clean(slot[3][0]) if qtype == 7 and len(slot) > 3 and isinstance(slot[3], list) and slot[3] else ""
            questions.append({
                "entry_id": f"entry.{slot[0]}",
                "title": f"{title} — {row}" if row else title,
                "description": desc,
                "type": QTYPE.get(qtype, "text"),
                "options": [o["label"] for o in opts if o["label"]],
                "has_other": any(o["other"] for o in opts),
                "required": len(slot) > 2 and slot[2] == 1,
                "date_has_year": not (qtype == 9 and len(slot) > 7 and isinstance(slot[7], list) and len(slot[7]) > 1 and slot[7][1] == 0),
                "date_has_time": qtype == 9 and len(slot) > 7 and isinstance(slot[7], list) and bool(slot[7][0]),
                "page": pages - 1,
            })
    fbzx = re.search(r'name="fbzx" value="([^"]+)"', html)
    return {"title": _clean(info[8] if len(info) > 8 and info[8] else info[0]) or "Google Form",
            "description": _clean(info[0]), "questions": questions, "pages": pages, "needs_login": needs_login,
            "view_url": view_url, "submit_url": submit_url, "fbzx": fbzx.group(1) if fbzx else ""}


# ───────────────────────────── 2. Mapping data to questions ─────────────────────────────
# Rule mapper: used when Gemini is unavailable, and to fill anything Gemini left empty.
RULES: List[Tuple[str, str]] = [
    (r"father", "father_name"), (r"mother", "mother_name"), (r"aadhaa?r|\buid\b", "aadhaar_number"),
    (r"nationality|citizenship", "nationality"), (r"present|correspondence|current address", "present_address"),
    (r"permanent address|\baddress\b", "permanent_address"),
    (r"(10th|tenth|class ?10|matric|secondary school).*(roll)|roll.*(10th|tenth|class ?10|matric)", "class10_roll"),
    (r"(12th|twelfth|class ?12|higher secondary|intermediate).*(roll)|roll.*(12th|twelfth|class ?12|higher secondary|intermediate)", "class12_roll"),
    (r"(10th|tenth|class ?10|matric).*board|board.*(10th|tenth|class ?10|matric)", "class10_board"),
    (r"(12th|twelfth|class ?12|higher secondary|intermediate).*board|board.*(12th|twelfth|class ?12|intermediate)", "class12_board"),
    (r"qualification", "highest_qualification_level"), (r"degree|course", "graduation_degree"),
    (r"year of passing|passing year", "graduation_year"), (r"percent|marks|cgpa|aggregate", "graduation_percentage"),
    (r"gender|\bsex\b", "gender"), (r"mobile|phone|contact", "mobile"), (r"e-?mail", "email"),
    (r"date of birth|\bdob\b|birth ?date", "dob"), (r"category|caste|community|reservation", "category"),
    (r"\bstate\b|domicile", "state"), (r"disab|pwbd|pwd", "pwbd"),
    (r"name", "full_name"),
]


def rule_map(questions: List[Dict[str, Any]], cand: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for q in questions:
        t = q["title"].lower()
        for rx, key in RULES:
            if re.search(rx, t) and cand.get(key) not in (None, ""):
                out[q["entry_id"]] = cand[key]
                break
    return out


MAP_PROMPT = """You fill Indian government-job application forms. For each question pick the value from the
candidate data. Rules: map "Full Name"/"Candidate Name" to the candidate (never the father/mother);
roll numbers are numbers, boards are board names; for radio/dropdown/checkbox answer with one of the listed options
EXACTLY (e.g. category "UR" → "General" when that is the option); dates as DD/MM/YYYY; if the data does not contain
the answer, use null — never invent. Return ONLY a JSON object {"entry.ID": value-or-null, ...}.

QUESTIONS:
"""


def gemini_post(body: Dict[str, Any]) -> Dict[str, Any]:
    """generateContent with model fallback on 429/404/timeouts."""
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("no GEMINI_API_KEY")
    reasons = []
    for model in list(dict.fromkeys([GEMINI_MODEL] + GEMINI_FALLBACK_MODELS)):
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code not in (429, 404):
                raise
            reasons.append(f"{model}: {e.code}")
        except (TimeoutError, OSError) as e:
            reasons.append(f"{model}: {type(e).__name__}")
    raise RuntimeError("Gemini unavailable — " + ", ".join(reasons))


def gemini_map(questions: List[Dict[str, Any]], cand: Any) -> Dict[str, Any]:
    qs = [{k: q[k] for k in ("entry_id", "title", "description", "type", "options", "required")} for q in questions]
    cand_text = cand if isinstance(cand, str) else json.dumps(cand, ensure_ascii=False)
    body = {"contents": [{"role": "user", "parts": [{"text": MAP_PROMPT + json.dumps(qs, ensure_ascii=False)
                                                     + "\n\nCANDIDATE DATA:\n" + cand_text[:6000]}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 4096, "responseMimeType": "application/json"}}
    data = gemini_post(body)
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    out = json.loads(text)
    return {k: v for k, v in out.items() if isinstance(k, str) and k.startswith("entry.")} if isinstance(out, dict) else {}


# ───────────────────────────── 3. Validating against the form ─────────────────────────────
SYNONYMS = {"ur": "general", "gen": "general", "unreserved": "general", "obc-ncl": "obc", "obc ncl": "obc",
            "m": "male", "f": "female", "indian": "india", "b.tech": "graduation", "btech": "graduation",
            "b.e": "graduation", "b.a": "graduation", "b.sc": "graduation", "b.com": "graduation",
            "bachelor": "graduation", "graduate": "graduation", "m.tech": "post graduation", "m.a": "post graduation",
            "m.sc": "post graduation", "postgraduate": "post graduation", "post graduate": "post graduation",
            "phd": "doctorate", "ph.d": "doctorate", "state": "state board"}


def match_option(value: str, options: List[str]) -> Optional[str]:
    v = _clean(value).lower()
    if not v:
        return None
    low = {o.lower(): o for o in options}
    if v in low:
        return low[v]
    head = re.split(r"[\s(]", v)[0]
    for cand in (SYNONYMS.get(v), SYNONYMS.get(head)):
        if cand and cand in low:
            return low[cand]
    close = difflib.get_close_matches(v, list(low), n=1, cutoff=0.8)
    if close:
        return low[close[0]]
    for o_low, o in low.items():  # "CBSE Board" → "CBSE"
        if o_low and re.search(rf"\b{re.escape(o_low)}\b", v):
            return o
    return None


def parse_date(raw: Any) -> Optional[date]:
    s = _clean(raw)
    for f in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def validate(q: Dict[str, Any], raw: Any) -> Tuple[str, Any, str]:
    """Returns (status, value, reason). status: filled | missing | invalid | skipped."""
    empty = raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []
    if empty:
        return ("missing", None, "required — no data for it") if q["required"] else ("skipped", None, "optional, no data")
    t, title = q["type"], q["title"].lower()
    if t in ("radio", "dropdown", "scale"):
        hit = match_option(str(raw), q["options"])
        if hit:
            return "filled", hit, ""
        if q["has_other"]:
            return "filled", {"other": _clean(raw)}, "answered as 'Other'"
        return "invalid", _clean(raw), "must be one of: " + ", ".join(q["options"])
    if t == "checkbox":
        items = raw if isinstance(raw, list) else re.split(r"[,;/]", str(raw))
        hits = [match_option(str(i), q["options"]) for i in items if _clean(i)]
        if hits and all(hits):
            return "filled", list(dict.fromkeys(hits)), ""
        return "invalid", _clean(raw), "choose from: " + ", ".join(q["options"])
    if t == "date":
        d = parse_date(raw)
        return ("filled", d.strftime("%d/%m/%Y"), "") if d else ("invalid", _clean(raw), "use DD/MM/YYYY")
    if t == "time":
        m = re.match(r"^(\d{1,2}):(\d{2})", _clean(raw))
        return ("filled", f"{int(m.group(1)):02d}:{m.group(2)}", "") if m else ("invalid", _clean(raw), "use HH:MM")
    if t == "file":
        return "invalid", None, "file uploads need a signed-in respondent — upload it on the form yourself"
    val = _clean(raw) if t != "paragraph" else str(raw).strip()
    if re.search(r"mobile|phone|contact no", title):
        digits = re.sub(r"\D", "", val)[-10:]
        return ("filled", digits, "") if re.fullmatch(r"[6-9]\d{9}", digits) else ("invalid", val, "10-digit Indian mobile number")
    if re.search(r"e-?mail", title):
        return ("filled", val.lower(), "") if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", val, re.I) else ("invalid", val, "valid email")
    if re.search(r"aadhaa?r", title):
        digits = re.sub(r"\D", "", val)
        if re.search(r"last ?4|last four", title):
            return ("filled", digits[-4:], "") if len(digits) >= 4 else ("invalid", val, "last 4 digits")
        return ("filled", digits, "") if len(digits) == 12 else ("invalid", val, "12-digit Aadhaar number")
    return "filled", val, ""


def plan_answers(form: Dict[str, Any], cand: Any, answers: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """Explicit user answers > Gemini > rules. Returns (fields, engine)."""
    engine = "rules"
    mapped: Dict[str, Any] = {}
    try:
        mapped = gemini_map(form["questions"], cand)
        engine = "gemini"
    except Exception:
        pass
    cand_dict = cand if isinstance(cand, dict) else {}
    for k, v in rule_map(form["questions"], cand_dict).items():
        if mapped.get(k) in (None, ""):
            mapped[k] = v
    mapped.update({k: v for k, v in answers.items() if v not in (None, "")})
    fields = []
    for q in form["questions"]:
        status, value, reason = validate(q, mapped.get(q["entry_id"]))
        fields.append({**{k: q[k] for k in ("entry_id", "title", "type", "options", "required")},
                       "value": value, "status": status, "reason": reason})
    return fields, engine


# ───────────────────────────── 4. Pre-fill link and submission ─────────────────────────────
def encode_answers(form: Dict[str, Any], fields: List[Dict[str, Any]], prefill: bool) -> List[Tuple[str, str]]:
    qmap = {q["entry_id"]: q for q in form["questions"]}
    out: List[Tuple[str, str]] = []
    for f in fields:
        if f["status"] != "filled":
            continue
        e, v, q = f["entry_id"], f["value"], qmap[f["entry_id"]]
        if q["type"] == "date":
            d = parse_date(v)
            if prefill:
                out.append((e, d.isoformat()))
            else:
                out += [(f"{e}_day", str(d.day)), (f"{e}_month", str(d.month))]
                if q["date_has_year"]:
                    out.append((f"{e}_year", str(d.year)))
        elif q["type"] == "time":
            hh, mm = v.split(":")
            out += [(f"{e}_hour", hh), (f"{e}_minute", mm)]
        elif isinstance(v, dict) and "other" in v:
            out += [(e, "__other_option__"), (f"{e}.other_option_response", v["other"])]
        elif isinstance(v, list):
            out += [(e, x) for x in v]
        else:
            out.append((e, str(v)))
    return out


def submit_form(form: Dict[str, Any], fields: List[Dict[str, Any]], email: str = "") -> Tuple[bool, str]:
    pairs = encode_answers(form, fields, prefill=False)
    pairs += [("fvv", "1"), ("pageHistory", ",".join(str(i) for i in range(form["pages"])))]
    if form.get("fbzx"):
        pairs.append(("fbzx", form["fbzx"]))
    if email:
        pairs.append(("emailAddress", email))  # used only if the form collects respondent emails
    req = urllib.request.Request(form["submit_url"], data=urllib.parse.urlencode(pairs).encode("utf-8"), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, f"Google rejected the response (HTTP {e.code}) — a value broke one of the form's rules"
    # A rejected response re-renders the form with its input fields; the confirmation page has none and links
    # back with usp=form_confirm (true even when the form has a custom confirmation message).
    if "usp=form_confirm" in html or 'name="entry.' not in html:
        return True, "Google recorded the response"
    return False, "Google showed the form again instead of the confirmation page (a value was not accepted)"


def fill(form_url: str, cand: Any, answers: Dict[str, Any], submit: bool) -> Dict[str, Any]:
    form = read_form(form_url)
    if form["needs_login"]:
        return {"ok": False, "submitted": False, "form_title": form["title"], "fields": [], "prefill_url": form["view_url"],
                "message": "This form requires Google sign-in (or has a file-upload question), so it can't be filled "
                           "automatically. Open it and fill it yourself."}
    if not form["questions"]:
        return {"ok": False, "submitted": False, "form_title": form["title"], "fields": [], "prefill_url": form["view_url"],
                "message": "This form has no questions I can fill."}
    fields, engine = plan_answers(form, cand, answers)
    prefill = form["view_url"] + "?usp=pp_url&" + urllib.parse.urlencode(encode_answers(form, fields, prefill=True))
    blocking = [f for f in fields if f["status"] in ("missing", "invalid")]
    filled = sum(1 for f in fields if f["status"] == "filled")
    base = {"form_title": form["title"], "fields": fields, "prefill_url": prefill}
    if blocking or not submit:
        msg = (f"{filled}/{len(fields)} answered ({engine} mapping)"
               + (f"; {len(blocking)} need your input" if blocking else "; ready to submit"))
        return {**base, "ok": not blocking, "submitted": False, "message": msg}
    email = next((f["value"] for f in fields if f["status"] == "filled" and re.search(r"e-?mail", f["title"], re.I)), "")
    ok, msg = submit_form(form, fields, email)
    return {**base, "ok": ok, "submitted": ok, "message": f"{msg} · {filled}/{len(fields)} answered ({engine} mapping)"}


# ───────────────────────────── 5. Protocols ─────────────────────────────
form_proto = Protocol(name="FormFillProtocol", version="2.0.0")


@form_proto.on_message(model=FormFillRequest, replies={FormFillResponse})
async def handle_fill(ctx: Context, sender: str, msg: FormFillRequest):
    t0 = time.time()
    try:
        cand = json.loads(msg.candidate_json or "{}")
        answers = json.loads(msg.answers_json or "{}")
        r = await asyncio.to_thread(fill, msg.form_url, cand, answers, msg.submit)
    except Exception as e:
        r = {"ok": False, "submitted": False, "form_title": "Google Form", "fields": [], "prefill_url": "",
             "message": f"{type(e).__name__}: {str(e)[:200]}"}
    ctx.logger.info(f"fill {msg.request_id[:8]} submit={msg.submit} → ok={r['ok']} submitted={r['submitted']} "
                    f"in {time.time() - t0:.1f}s: {r['message']}")
    await ctx.send(sender, FormFillResponse(
        request_id=msg.request_id, ok=r["ok"], submitted=r["submitted"], form_title=r["form_title"][:200],
        fields_json=json.dumps(r["fields"], ensure_ascii=False), prefill_url=r["prefill_url"][:6000], message=r["message"]))


# Direct chat: "fill <form link> — name …, dob …". Missing details are asked for and remembered per sender.
chat_proto = Protocol(spec=chat_protocol_spec)
FORM_RX = re.compile(r"https?://(?:docs\.google\.com/forms|forms\.gle)/\S+")


def chat_reply(r: Dict[str, Any]) -> str:
    L = [f"## 📝 {r['form_title']}", r["message"], ""]
    for f in r["fields"]:
        icon = {"filled": "✅", "missing": "❗", "invalid": "⚠️", "skipped": "▫️"}[f["status"]]
        val = ", ".join(f["value"]) if isinstance(f["value"], list) else (
            f["value"]["other"] if isinstance(f["value"], dict) else f["value"])
        L.append(f"- {icon} **{f['title']}**" + (f": {val}" if f["status"] == "filled" else f" — {f['reason']}"))
    if not r["submitted"] and any(f["status"] in ("missing", "invalid") for f in r["fields"]):
        L += ["", "💬 Reply with the missing details (e.g. *Mobile 9876543210, Category OBC*) — I'll keep the form link."]
    if r.get("prefill_url") and not r["submitted"]:
        L += ["", f"[Open the pre-filled form]({r['prefill_url']})"]
    return "\n".join(L)


@chat_proto.on_message(ChatMessage)
async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    key = f"chat:{sender}"
    session = ctx.storage.get(key) or {}
    m = FORM_RX.search(text)
    if m:
        session = {"form_url": m.group(0), "details": FORM_RX.sub("", text).strip()}
    elif session.get("form_url") and text:
        session["details"] = (session.get("details", "") + "\n" + text).strip()
    else:
        reply = ("👋 **ApplyEase Form Filler (Agent 2)** — I fill any public Google Form from your details. Send the "
                 "form link plus your details, e.g. *Fill https://forms.gle/… Name Aarav Sharma, DOB 14/05/1998, "
                 "Category OBC, Mobile 9876543210*. I'll ask for anything missing before submitting. "
                 "For the full career assistant, talk to **@applyeasee**.")
        await ctx.send(sender, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                           content=[TextContent(type="text", text=reply)]))
        return
    try:
        r = await asyncio.to_thread(fill, session["form_url"], session["details"], {}, True)
        reply = chat_reply(r)
        if r["submitted"]:
            session = {}
    except Exception as e:
        reply = f"⚠️ Couldn't process that form: {type(e).__name__}: {str(e)[:200]}"
    ctx.storage.set(key, session)
    await ctx.send(sender, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                       content=[TextContent(type="text", text=reply)]))


@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(form_proto, publish_manifest=True)
agent.include(chat_proto, publish_manifest=True)
