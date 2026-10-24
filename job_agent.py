"""
AI Job Agent (v1)
-----------------
1. parse   : CV (pdf/docx/txt) -> structured master profile (profile.json)
2. search  : free job APIs (Remotive, RemoteOK, Arbeitnow) using queries built from your profile
3. match   : Claude scores each job against your profile (0-100)
4. tailor  : Claude rewrites your CV for the job WITHOUT inventing anything
5. apply   : opens the application page with Playwright, pre-fills fields, YOU click submit

Usage:
    python job_agent.py parse  my_cv.pdf
    python job_agent.py run    [--min-score 60] [--max-jobs 10] [--location "Morocco"]
    python job_agent.py apply  <job_id>
"""
import argparse, json, os, re, sys, hashlib
from pathlib import Path

import time
import requests

MODEL = "claude-sonnet-5-5"
DATA = Path(os.environ.get("JOB_AGENT_HOME", "."))
OUT = DATA / "output"
OUT.mkdir(parents=True, exist_ok=True)
PROFILE_PATH = DATA / "profile.json"
JOBS_PATH = OUT / "jobs.json"
SEEN_PATH = OUT / "seen.json"


def get_client():
    import anthropic  # only needed if you use a Claude key
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment


# ---------- free option: Google Gemini (key from aistudio.google.com) ----------
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_gemini_models = []


def gemini_candidates():
    """Ask Google which models this key can use, so we never depend on a retired model name."""
    global _gemini_models
    if _gemini_models:
        return _gemini_models
    key = os.environ["GEMINI_API_KEY"]
    r = requests.get(f"{GEMINI_BASE}/models", params={"key": key, "pageSize": 200}, timeout=30)
    r.raise_for_status()
    names = []
    for m in r.json().get("models", []):
        n = m["name"].split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if "flash" not in n or any(x in n for x in ("image", "tts", "live", "audio", "embed", "2.5", "2.0", "1.5", "robotics", "computer")):
            continue
        names.append(n)
    # Order: stable "latest" aliases and lite/flash models first (these are the ones usually on the free tier);
    # experimental "omni" models last.
    prefs = ["flash-lite-latest", "flash-latest", "3.1-flash-lite", "3-flash", "flash-lite", "flash"]
    def rank(n):
        r = next((i for i, p in enumerate(prefs) if p in n), len(prefs))
        return (("omni" in n) * 100 + r, n)
    names.sort(key=rank)
    if os.environ.get("GEMINI_MODEL"):
        names.insert(0, os.environ["GEMINI_MODEL"])
    if not names:
        raise RuntimeError("No usable Gemini model found for this key.")
    _gemini_models = names
    print(f"Using Gemini models (in order): {names[:3]}")
    return names


def ask_gemini(system, user, max_tokens):
    key = os.environ["GEMINI_API_KEY"]
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens * 2}}
    last = None
    models = gemini_candidates()
    for model in list(models)[:6]:
        for attempt in range(2):
            r = requests.post(f"{GEMINI_BASE}/models/{model}:generateContent", json=body,
                              headers={"x-goog-api-key": key}, timeout=120)
            if r.status_code == 200:
                parts = r.json()["candidates"][0]["content"]["parts"]
                if models[0] != model:           # remember the model that works
                    models.remove(model); models.insert(0, model)
                    print(f"  -> using {model}")
                return "".join(p.get("text", "") for p in parts)
            last = f"{r.status_code}: {r.text[:150]}"
            # "limit: 0" means this model has no free quota for you: skip it right away
            if r.status_code == 429 and "limit: 0" not in r.text and attempt == 0:
                time.sleep(20)                   # normal per-minute limit: wait once, retry
                continue
            break
        print(f"  ! {model} unavailable ({r.status_code}); trying next model")
    raise RuntimeError(f"All Gemini models failed. Last error: {last}")


def ask_claude(system: str, user: str, max_tokens: int = 4000) -> str:
    if os.environ.get("GEMINI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        time.sleep(4)  # stay under free-tier requests-per-minute
        return ask_gemini(system, user, max_tokens)
    msg = get_client().messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def ask_json(system: str, user: str, max_tokens: int = 4000):
    text = ask_claude(system + "\nReturn ONLY valid JSON, no markdown fences.", user, max_tokens)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def read_cv(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(p).pages)
    if p.suffix.lower() == ".docx":
        import docx
        return "\n".join(par.text for par in docx.Document(p).paragraphs)
    return p.read_text(encoding="utf-8")


# ---------- 1. parse ----------
def parse_cv(path: str):
    raw = read_cv(path)
    profile = ask_json(
        "You extract structured data from CVs. Never add information that is not in the CV.",
        f"""Convert this CV to JSON with keys:
name, email, phone, location, links (list), summary, target_titles (list of 5 job titles this person
fits, based ONLY on their real experience), skills (list), experience (list of
{{company, title, start, end, bullets[]}}), education (list), projects (list of {{name, description}}),
languages (list).

CV:
{raw}""",
    )
    PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {PROFILE_PATH}. Review it and fix anything wrong - everything else builds on it.")


# ---------- 2. search ----------
def job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:8]


def search_remotive(q):
    r = requests.get("https://remotive.com/api/remote-jobs", params={"search": q, "limit": 20}, timeout=20)
    return [{"title": j["title"], "company": j["company_name"], "location": j["candidate_required_location"],
             "url": j["url"], "description": re.sub(r"<[^>]+>", " ", j["description"])[:6000]}
            for j in r.json().get("jobs", [])]


def search_arbeitnow(q):
    r = requests.get("https://www.arbeitnow.com/api/job-board-api", timeout=20)
    words = q.lower().split()
    out = []
    for j in r.json().get("data", []):
        if any(w in (j["title"] + j["description"]).lower() for w in words):
            out.append({"title": j["title"], "company": j["company_name"], "location": j["location"],
                        "url": j["url"], "description": re.sub(r"<[^>]+>", " ", j["description"])[:6000]})
    return out[:20]


def search_remoteok(q):
    r = requests.get("https://remoteok.com/api", headers={"User-Agent": "job-agent"}, timeout=20)
    words = q.lower().split()
    out = []
    for j in r.json()[1:]:
        blob = (j.get("position", "") + " " + " ".join(j.get("tags", []))).lower()
        if any(w in blob for w in words):
            out.append({"title": j["position"], "company": j["company"], "location": j.get("location", "Remote"),
                        "url": j["url"], "description": re.sub(r"<[^>]+>", " ", j.get("description", ""))[:6000]})
    return out[:20]


def search_jobs(profile, location=None):
    jobs = {}
    for title in profile.get("target_titles", [])[:5]:
        for fn in (search_remotive, search_remoteok, search_arbeitnow):
            try:
                for j in fn(title):
                    if location and location.lower() not in j["location"].lower() and "remote" not in j["location"].lower() \
                            and "worldwide" not in j["location"].lower():
                        continue
                    jobs[job_id(j["url"])] = {**j, "id": job_id(j["url"])}
            except Exception as e:
                print(f"  ! {fn.__name__} failed for '{title}': {e}")
    print(f"Found {len(jobs)} unique jobs.")
    return list(jobs.values())


# ---------- 3. match ----------
def score_job(profile, job):
    res = ask_json(
        "You are a strict recruiter. Score how well the candidate fits the job using ONLY facts in the profile.",
        f"""PROFILE:\n{json.dumps(profile, ensure_ascii=False)}\n\nJOB: {job['title']} at {job['company']}
({job['location']})\n{job['description']}\n
Return {{"score": 0-100, "matching": [..], "missing": [..], "reason": "one sentence"}}""",
        max_tokens=800,
    )
    return res


# ---------- 4. tailor ----------
def tailor(profile, job):
    cv_md = ask_claude(
        "You tailor CVs. RULES: use ONLY facts present in the profile. Never invent skills, employers, dates, "
        "numbers or tools. You may reorder, select, and reword to mirror the job's keywords when truthful. "
        "Output the CV in clean Markdown, max 1 page of content.",
        f"PROFILE:\n{json.dumps(profile, ensure_ascii=False)}\n\nJOB: {job['title']} at {job['company']}\n{job['description']}",
    )
    letter = ask_claude(
        "Write a concise, specific cover letter (under 200 words). Use ONLY facts in the profile. No clichés.",
        f"PROFILE:\n{json.dumps(profile, ensure_ascii=False)}\n\nJOB: {job['title']} at {job['company']}\n{job['description']}",
        max_tokens=800,
    )
    folder = OUT / job["id"]
    folder.mkdir(exist_ok=True)
    (folder / "cv.md").write_text(cv_md, encoding="utf-8")
    (folder / "cover_letter.md").write_text(letter, encoding="utf-8")
    make_pdf(cv_md, folder / "cv.pdf")
    return folder


def make_pdf(markdown_text: str, path: Path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    styles = getSampleStyleSheet()
    story = []
    for line in markdown_text.splitlines():
        line = line.strip()
        if not line:
            story.append(Spacer(1, 6)); continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        if line.startswith("#"):
            story.append(Paragraph(line.lstrip("# "), styles["Heading2"]))
        elif line.startswith(("-", "*")):
            story.append(Paragraph("• " + line[1:].strip(), styles["BodyText"]))
        else:
            story.append(Paragraph(line, styles["BodyText"]))
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=40, rightMargin=40, topMargin=40, bottomMargin=40).build(story)


# ---------- 5. apply (semi-automatic) ----------
def apply_to(job_id_: str, wait1=None, wait2=None):
    wait1 = wait1 or (lambda m: input(m))
    wait2 = wait2 or (lambda m: input(m))
    from playwright.sync_api import sync_playwright
    jobs = {j["id"]: j for j in json.loads(JOBS_PATH.read_text(encoding="utf-8"))}
    job = jobs[job_id_]
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    folder = OUT / job_id_
    first, _, last = profile["name"].partition(" ")
    guesses = {
        "first": first, "last": last, "name": profile["name"],
        "email": profile.get("email", ""), "phone": profile.get("phone", ""),
        "linkedin": next((l for l in profile.get("links", []) if "linkedin" in l), ""),
        "cover": (folder / "cover_letter.md").read_text(encoding="utf-8"),
    }
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=False)  # uses your installed Chrome
        except Exception:
            browser = p.chromium.launch(channel="msedge", headless=False)
        page = browser.new_page()
        page.goto(job["url"])
        wait1("Click the 'Apply' button on the page if needed, then press OK here to pre-fill.")
        for el in page.query_selector_all("input, textarea"):
            ident = " ".join(filter(None, [el.get_attribute(a) for a in ("name", "id", "placeholder", "aria-label")])).lower()
            typ = (el.get_attribute("type") or "text").lower()
            try:
                if typ == "file" and ("resume" in ident or "cv" in ident):
                    el.set_input_files(str(folder / "cv.pdf"))
                elif "first" in ident and "name" in ident: el.fill(guesses["first"])
                elif "last" in ident and "name" in ident: el.fill(guesses["last"])
                elif "name" in ident and "company" not in ident: el.fill(guesses["name"])
                elif "email" in ident: el.fill(guesses["email"])
                elif "phone" in ident: el.fill(guesses["phone"])
                elif "linkedin" in ident: el.fill(guesses["linkedin"])
                elif "cover" in ident or "message" in ident: el.fill(guesses["cover"])
            except Exception:
                pass
        wait2("Review every field, answer remaining questions, SUBMIT yourself, then press OK to close the browser.")
        browser.close()


# ---------- main ----------
def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def run(min_score, max_jobs, location):
    """One search cycle. Skips jobs already processed in earlier cycles. Returns list of new matches."""
    if not PROFILE_PATH.exists():
        raise RuntimeError("No profile yet - load your CV first.")
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    seen = set(load_json(SEEN_PATH, []))
    jobs = [j for j in search_jobs(profile, location) if j["id"] not in seen][: max_jobs * 3]
    print(f"{len(jobs)} new jobs to evaluate.")
    kept = []
    for j in jobs:
        try:
            j.update(score_job(profile, j))
        except Exception as e:
            print(f"  ! scoring failed: {e}")
            continue
        seen.add(j["id"])
        print(f"[{j['score']:>3}] {j['title']} @ {j['company']}")
        if j["score"] >= min_score:
            kept.append(j)
    kept = sorted(kept, key=lambda x: -x["score"])[:max_jobs]
    for j in kept:
        tailor(profile, j)
        j["status"] = "ready"
    SEEN_PATH.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    all_jobs = load_json(JOBS_PATH, [])
    all_jobs.extend(kept)
    JOBS_PATH.write_text(json.dumps(all_jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(kept)} new jobs ready.")
    return kept


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("parse"); sp.add_argument("cv")
    sr = sub.add_parser("run")
    sr.add_argument("--min-score", type=int, default=60)
    sr.add_argument("--max-jobs", type=int, default=10)
    sr.add_argument("--location", default=None)
    sa = sub.add_parser("apply"); sa.add_argument("job_id")
    a = ap.parse_args()
    if a.cmd == "parse": parse_cv(a.cv)
    elif a.cmd == "run": run(a.min_score, a.max_jobs, a.location)
    elif a.cmd == "apply": apply_to(a.job_id)
