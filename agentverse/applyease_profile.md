# ApplyEase — Government Job Eligibility & Career Agent (India)

**ApplyEase tells Indian job-seekers which government jobs they can apply for today, what stands between them and better posts, and how to prepare — starting from just their age and qualification.**

It covers central recruitment from **SSC** (CHSL, CGL, GD Constable, MTS, CPO Sub-Inspector), **Railways / RRB** (NTPC, Group D, Junior Engineer), **UPSC** (NDA, CDS, Civil Services), **IBPS** (Bank PO) and **State PSC / selection boards**.

---

## What you get

Send your profile in plain language, e.g. *"I am 18, 12th pass, OBC — what jobs are available?"* The agent replies with a 3-tier assessment:

1. **✅ Tier 1 — Apply now.** Posts you are eligible for today, with vacancies, pay level, last date and days left, *your* application fee, the official notification and the apply portal.
2. **🪜 Tier 2 — Career ladder & gap analysis.** Higher posts you can't apply for yet, the exact gap (e.g. *"any bachelor's degree, 3 years"* or *"engineering diploma"*), the age and year you become eligible, and roughly how many attempts your age window allows.
3. **📅 Tier 3 — Preparation roadmap from today.** A month-by-month plan with calendar dates for your main target exam, plus skill or physical-test practice (typing speed, running), and which degree or diploma to start in parallel.

Then it goes past advice:

4. **📄 Reads the official notification with Gemini.** For a post with a notification PDF (try the **ADRB CGLE (Demo)** vacancy), it extracts the age limit and cut-off date, relaxations, qualification, closing date, required documents and the application form's fields, and checks you against *those* rules.
5. **📑 Checks your documents.** Upload a photo or PDF with **+**: Gemini Vision reads the name, parents' names, date of birth and issue dates (only the last 4 Aadhaar digits are ever kept). The audit flags Class 10 vs Aadhaar name mismatches (initials, missing middle name, spelling, surname change after marriage), date-of-birth conflicts, an outdated or State-format OBC-NCL / EWS certificate, and missing documents — and drafts a name-declaration affidavit.
6. **✍️ Fills the application form — with a second agent.** Tap **Fill application form (Agent 2)**: the **ApplyEase Form Filler** agent reads the Google Form, fills what it can from your profile and documents, and sends back the questions it couldn't answer. You get a card with exactly those questions (with the form's own options), then a review card, and only after you confirm does Agent 2 submit it. You can also paste any Google Form link.

## Multi-agent architecture

| Agent | Role |
|---|---|
| **ApplyEase** (this agent, `@applyeasee`) | Conversation, eligibility engine, notification reading, Gemini Vision document reading, audit, cards |
| **ApplyEase Form Filler** (`agent1q2dtw0yzaf5ssvn590kgue3m8dknn8j2pmw2qzsgn0h900t9neuds3rdwe5`) | Reads any public Google Form, maps the data with Gemini, validates every value, previews or submits |

They talk over `FormFillProtocol v2.0.0`: preview → missing fields back to ApplyEase → the user answers in a card → review → submit.

## What it checks

- **Education hierarchy:** 10th < 12th < Diploma < Graduate < Post-Graduate, plus stream rules (e.g. RRB JE needs an *engineering* diploma or degree).
- **Category age relaxation:** OBC +3, SC/ST +5, PwBD +10 (added on top of category). It says when you qualify only because of relaxation, notes where relaxation doesn't apply (NDA, CDS), and lists the certificate you'll need (e.g. OBC non-creamy-layer certificate in the Central Govt format).
- **Fees by category and gender:** e.g. women and SC/ST are exempt for SSC and UPSC, and RRB fees are refundable.
- **Physical, gender and marital rules for police and defence posts:** height cut-offs by gender (and ST standards), running and other physical-test standards, the LMV driving licence for Delhi Police SI (men), unmarried status for NDA/CDS, Physics + Maths for Navy/Air Force entries, and posts not open to PwBD candidates.

## Example messages

- `I am 18, 12th pass, what jobs are available?`
- `24 yr B.Tech civil graduate, OBC female`
- `I am 26, OBC male graduate, 172 cm, no driving licence`
- `details SSC CPO SI` — full eligibility check and physical screening for one post
- `roadmap SSC CHSL` — month-by-month preparation plan
- `I am female` / `my height is 165 cm` — updates your saved profile and recalculates
- `What documents do I need for SSC CGL?` — the document checklist; then upload each one with **+**
- `marksheet name Rajesh Kumar Sharma, aadhaar name Rajesh K Sharma, OBC certificate issued 15/01/2025` — a typed document audit
- `fill the application form` — Agent 2 fills the open post's form (or paste any Google Form link)
- `reset` — clears your profile and extracted document details

The agent remembers your profile during the conversation, so you can add details one at a time.

## Data

Vacancies come from the ApplyEase crawler, which syncs official notifications to a database on a schedule. If the database can't be reached, the agent answers from a built-in list. Each full assessment shows the date the data was last updated.

## Limits — please read

- **Guidance, not a final verdict.** Always confirm dates, relaxations and physical standards in the official notification before applying or paying a fee.
- **Age is calculated from the age you give, as of today.** Notifications fix age on a cut-off date, so check the official date if you are near a limit.
- Regional height relaxations (hill areas, North-East states, etc.) and some categories (ex-servicemen, departmental candidates, widows) are mentioned but not calculated.
- State OBC lists differ from the central list. If your caste is OBC only in your state's list, central jobs (SSC, RRB, UPSC) treat you as UR.

## Technical

- **Protocols:** Agent Chat Protocol (`ChatMessage`, `ChatAcknowledgement`, `TextContent`, `MetadataContent`, `ResourceContent` for uploads) and `FormFillProtocol v2.0.0` to the Form Filler agent.
- **Replies:** Markdown text plus an **ASI:One interactive card** in every reply — `form` (profile, document details, missing form questions), `carousel` (eligible posts), `detail` (post, roadmap), `review` (affidavit, application) and `custom` (document checklist, audit, career ladder, submission receipt).
- **AI:** Google Gemini reads notification PDFs, document photos and answers free-form questions; eligibility and the audit verdicts come from deterministic rules, so a model error can't invent a post, date or relaxation.
- **Privacy:** uploaded files are read once and not stored; only names and dates are kept, and `reset` deletes them. Only the last 4 Aadhaar digits are kept or shown.
- **Keywords:** government jobs India, sarkari naukri, SSC, RRB, railway jobs, UPSC, NDA, CDS, IBPS, bank PO, police SI, constable, 10th pass jobs, 12th pass jobs, graduate jobs, eligibility, age relaxation, OBC, SC, ST, EWS, PwBD, exam preparation, career roadmap.
