# Design notes

Why the system is built the way it is. Each section names the problem, the choice made, and the trade-off.

---

## 1. Getting *all* the tenders, not 20

**Problem.** The portal's "Latest Tenders" table renders 20 rows. Paging through the HTML is slow and fragile.

**What I found.** The page is an AngularJS app. Its controller (`openareaTenderList`) loads the full list of active tenders into `$scope.allTenderList` (about 700 rows, 50+ departments), together with the department list and the category masters. The table is only a view of that list.

**Choice.** One Playwright page load, then `angular.element(el).scope()` to read the whole list as JSON. One request gives every open tender.

**Detail data.** Value, EMD, fees, pre-bid date, qualification rows and attachments come from the endpoint the portal's own "View" pop-up calls (`previewTenderByTenderId`). It refuses requests from outside the browser, so the calls go through the page's own Angular `$http`, from inside the loaded page:
- only for tenders not seen before
- one at a time, with a 1.5 s pause between calls
- a normal run touches the portal for well under a minute

**Trade-off.** If the portal changes its Angular structure, the scraper breaks loudly ("Tender list did not load") instead of silently returning partial data. I chose that on purpose.

---

## 2. Never send the same alert twice

**Design.** A `deliveries` table with the primary key **(subscriber_id, tender_id, reason)**:

| reason | sent when |
|---|---|
| `new` | the first time a tender appears after the baseline |
| `closing:<close_at>` | once, when the deadline is within 3 days |
| `extended:<new close_at>` | when the closing date moves later, recorded in `tender_events` |

Because the closing date is part of the key, an extension automatically "re-arms" the reminder for the new date. No extra flags are needed.

**Baseline.** On the very first scrape everything already on the portal is marked as the baseline. Without that, the first run would send about 700 "new" tenders.

**Closed vs withdrawn.** A tender missing from the list is marked `closed` if its deadline has passed, otherwise `removed` (cancelled or withdrawn).

---

## 3. Filters that match how contractors think

- **Same kind is OR, different kinds are AND.** `dept ∈ {BCD, RWD}` AND `district ∈ {Patna, Purnia}` AND `size ∈ {₹50 L–2 Cr}`. With no filters a user gets everything.
- **District detection.**
  - Titles name towns and blocks, not districts ("Bounsi", "Kankarbagh", "Gayaji"), so 90+ aliases map to Bihar's 38 districts.
  - Longest match first, and each match is removed once found, so "Simri Bakhtiyarpur" (Saharsa) isn't also counted as "Bakhtiyarpur" (Patna).
- **District filter is strict.** A tender with no detectable district is *not* sent to someone who chose a district. They can tick "also send tenders with no district" if they want them.
- **Type of work** comes from the title, not the department, because departments do mixed work.
  - The work is named *before* the landmarks: in "RCC drain **from** the house of X **to** Y Bhawan", "Bhawan" is a landmark, not a building job. So only the words before `from / near / to / via …` are checked first.
  - The department is a fallback when the title says nothing.
- **Tender size bands:** under ₹50 L, ₹50 L–2 Cr, ₹2–10 Cr, ₹10 Cr+. A tender with no published value always passes a size filter, so nothing is hidden by missing data.
- **Sand-ghat / mining e-auctions** are hidden for everyone. They aren't contractor work, and they flooded the lists (82 of 734).

---

## 4. "Who can bid": rules first, AI second

**Problem.** The most useful fact for a contractor is whether they qualify. That information is free text in the tender's "General Particulars" rows, or only inside the NIT PDF.

**Step 1: rules (free, instant, explainable).**
- `eligibility.py` sorts each qualification row into topics with regex: turnover, experience, bid capacity, net worth, bank credit, machinery, staff, registration and manufacturer.
- It pulls out "40% of Estimated Cost Value" and converts it to rupees with the tender's own estimated value: 40% × ₹111 Cr → **≥ ₹44.43 Cr**.
- It parses bid capacity formulas (`A×N×3−B`), lists of machinery with quantities, staff roles and contractor class.
- Edge cases handled: "up to two contracts with **combined** value ≥ 50%" vs "**each**", supply tenders ("supplied ≥ 50% of tendered quantity"), and "Building Construction **Department**" vs the corporation with the same name.
- Result: a structured summary for **52%** of open tenders, plus a document checklist for **100%**.

**Step 2: AI, only when needed (`nit_reader.py`).**
- For the rest, the rules exist only in the NIT, which is often a scanned Hindi page or an old Hindi font. Plain text extraction returns garbage for those.
- When a user taps such a tender, a separate process downloads the NIT and sends the first 15 pages to **Gemini (free tier)** as a PDF.
- It asks for a fixed JSON schema: open to, turnover, experience, bid capacity, machinery, staff, completion, bid validity, documents. The prompt demands real figures, never "see Annexure-5".

**Cost and reliability controls:**
- Each NIT is read **once**; the result is stored and shared with every later user.
- Daily limits overall and per user.
- If two people tap while a reading is in progress, both get that one reading.
- Model fallback chain: retry once on 503 (busy); move to the next model on 429 (quota) or 404 (retired).
- PDFs are always re-saved with `pypdf` before sending. Scanner-made files often have a broken structure the API rejects ("document has no pages").
- Up to 15 pages are sent to Gemini (pages cost little there) and 6 to Claude.

**Why not AI for everything?** Cost, speed and explainability. The regex result is instant and can be checked against the source text. The AI is used only where rules can't reach.

---

## 5. A bot contractors can actually use

Most users aren't comfortable typing commands, so the bot is tap-first:

- **Setup in 3 tap steps:** Departments → Districts → Tender size. Pickers edit the same message in place, so the chat doesn't fill up with screens.
- **Bottom keyboard:** My tenders · Departments · Districts · Browse all · My settings.
- **Plain-word search (`smart.py`):** `civil patna`, `BCD`, `rural works`, `nagar nigam`, `Patna, Purnia` or a tender ID. It matches department codes (`RCD` → `RCD_HQ`) and words in department names. With 2+ words it picks the closest name. Results include a "🔔 Alert me for these" button.
- **Repeat-tap protection:** the same request from the same chat within 60 s is answered once.
- **BOQ / NIT download:** the first request downloads from the portal and uploads to Telegram. After that the file is re-sent by Telegram `file_id`, instantly, without touching the portal again.

**Channel vs bot.** The public channel is the shop window: every new tender, as a clean list, no buttons. The bot is the product: personal filters, details, AI summaries and files. The channel's footer sends readers to the bot with a Tender ID.

---

## 6. Running unattended on a home Windows PC

- **No admin rights:** per-user Task Scheduler jobs, run hidden through `wscript run_hidden.vbs`.
  - Every 3 hours: scrape and alert.
  - Every 5 minutes: keep-alive.
  - Every morning: digest and owner report.
- **Exactly one bot:** a socket lock on a local port. Two pollers would make Telegram return `409 Conflict`.
- **Self-updating:** the bot checks the newest change time of its code and `.env` and exits when either changes. The keep-alive task starts the new version within 5 minutes.
- **Failures never lose an alert:**
  - If Telegram rejects the HTML, the message is resent as plain text.
  - A user who blocked the bot is marked inactive.
  - Network errors back off and retry.

---

## 7. Testing

41 pytest tests run on **recorded real responses** from the portal (`tests/fixtures/`), with no network needed. They cover:
- parsing and district/sector classification on real titles
- the no-repeat and deadline-extension logic, using a fake clock
- filter semantics, message splitting and Telegram HTML safety (a real outage was caused by an unescaped `<ID>` once)
- the full tap-through setup flow, plain-word search, repeat-tap protection
- NIT reading (once per tender, shared by everyone who asks, daily limits, model fallback) with the AI call faked
- BOQ download caching, and owner-only stats

---

## 8. What I'd do next

- Corrigendum alerts: the portal exposes `getPublishedCorrigendumByTenderId`.
- Award / L1-rate history, for pricing insight.
- Move from a home PC to a small cloud VM, and add monitoring.
- A Power BI dashboard on the SQLite data: tenders by department, district, value band and month.
