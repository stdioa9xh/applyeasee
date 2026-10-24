# ApplyEase Form Filler — fills any Google Form from your details

**Give it a Google Form link and your details; it reads the form, fills every question it can, tells you exactly what's missing, and submits only when everything is valid.**

Built for job and exam application forms, but works on any public Google Form.

## What it does

1. **Reads the whole form:** every page, question type (short and long answer, multiple choice, dropdown, checkboxes, date, time, scale, grid), its options and which questions are required.
2. **Maps your data to the questions with Gemini:** "Candidate's name as per matriculation" gets your name, not your father's; roll numbers stay numbers; your category "UR" becomes the form's option "General". Rules take over if Gemini is unavailable.
3. **Validates every value against the form:** options must exist (with an "Other" answer when the form allows it), dates are converted, mobile numbers must be 10 digits, emails must be valid, Aadhaar must have 12 digits (or 4 when the form asks for the last four).
4. **Previews or submits:** in preview it returns each question as filled, missing or invalid, plus a pre-filled link. It submits only when nothing is missing, and confirms that Google actually recorded the response.

Forms that require Google sign-in or a file upload can't be filled automatically — it says so and gives you the link.

## Use it

- **Through ApplyEase (`@applyeasee`):** pick a post, check your documents, then tap **Fill application form (Agent 2)**. ApplyEase shows you the missing questions as a card and a review card before anything is submitted.
- **Directly:** send `Fill https://forms.gle/… Name Aarav Sharma, DOB 14/05/1998, Category OBC, Mobile 9876543210`. It replies with what it filled and what's missing; reply with the missing details and it finishes the form.

## Technical

- **Protocols:** `FormFillProtocol v2.0.0` (`FormFillRequest` → `FormFillResponse`, with a per-question status list and a pre-filled link) and the Agent Chat Protocol.
- **Keywords:** google form filler, auto fill form, application form, job application, exam form, form automation.
