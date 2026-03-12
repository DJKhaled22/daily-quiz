#!/usr/bin/env python3
"""
Daily Quiz Generator
Pulls notes from Mac Notes app, generates MCQs via Claude API,
publishes to GitHub Pages, and emails the link.
"""

import os
import re
import json
import random
import subprocess
import smtplib
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from dotenv import load_dotenv
import anthropic

# Load environment variables
load_dotenv(Path(__file__).parent / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
GITHUB_REPO_PATH = os.getenv("GITHUB_REPO_PATH", os.path.expanduser("~/daily-quiz-repo"))
GITHUB_PAGES_URL = os.getenv("GITHUB_PAGES_URL")  # e.g. https://username.github.io/daily-quiz
EMAIL_TO = os.getenv("EMAIL_TO", "h.owainati@gmail.com")
NOTES_FILE = os.getenv("NOTES_FILE")  # if set, read notes from file instead of Apple Notes


# ─── 1. Notes Extraction ────────────────────────────────────────────────────

APPLESCRIPT = """
tell application "Notes"
    set allText to ""
    set maxNoteChars to 5000
    set excludedTitles to {"iRobot(on hold)", "User story mapping (on hold)", "Storyworthy (not finished)", "A gentleman in Moscow (finished)", "A visit from the goon squad (finished)"}
    try
        set theFolder to folder "Books"
        set theNotes to notes of theFolder
        repeat with theNote in theNotes
            set noteName to name of theNote
            set shouldSkip to false
            repeat with excl in excludedTitles
                if noteName is (excl as text) then
                    set shouldSkip to true
                    exit repeat
                end if
            end repeat
            if not shouldSkip then
                set noteBody to body of theNote
                set noteLen to length of noteBody
                if noteLen > maxNoteChars then
                    set noteBody to text 1 thru maxNoteChars of noteBody
                end if
                set allText to allText & noteBody & "\n\n---\n\n"
            end if
        end repeat
    on error
        -- folder not found, skip
    end try
    return allText
end tell
"""

def extract_notes() -> str:
    if NOTES_FILE:
        print(f"Reading notes from {NOTES_FILE}...")
        text = Path(NOTES_FILE).read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"Notes file {NOTES_FILE} is empty.")
        print(f"Extracted {len(text)} characters of notes.")
        return text
    print("Extracting notes from Mac Notes app...")
    result = subprocess.run(
        ["osascript", "-e", APPLESCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript failed: {result.stderr}")
    text = result.stdout.strip()
    if not text:
        raise ValueError("No notes found in 'Work Readings and Learnings' or 'Books' folders.")
    print(f"Extracted {len(text)} characters of notes.")
    return text


# ─── 2. Notes Splitting ─────────────────────────────────────────────────────

def split_notes(raw: str) -> list[str]:
    """Split raw notes text into individual note sections."""
    sections = [s.strip() for s in raw.split("\n\n---\n\n") if s.strip()]
    return sections


def clean_title(title: str) -> str:
    """Remove parenthetical suffixes like '(finished)' from titles."""
    return re.sub(r'\s*\(.*?\)', '', title).strip()


# ─── 3. Quiz Generation ─────────────────────────────────────────────────────

QUIZ_PROMPT_TEMPLATE = """You are a quiz creator. Below are {n} separate notes, each from a different book. Generate exactly one multiple choice question per note — {n} questions total.

Requirements:
- Each question must have exactly 4 options (A, B, C, D)
- Only one option is correct
- Include a brief explanation for the correct answer
- Do not number the options — just provide the text
- Question i must be based solely on Note i

Return ONLY a valid JSON array with this exact structure (no markdown, no commentary):
[
  {{
    "question": "...",
    "options": ["option A text", "option B text", "option C text", "option D text"],
    "answer": 0,
    "explanation": "...",
    "book": "...",
    "author": "...",
    "topic": "..."
  }}
]

The "answer" field is the 0-based index of the correct option.
The "book" and "author" fields should reflect the source book for that note.
The "topic" field should be a short genre or subject label (2–4 words, e.g. "World War I", "Behavioural Economics", "Ancient Rome").

{notes_block}"""


def generate_quiz(notes_list: list[str]) -> list[dict]:
    print("Generating quiz with Claude...")
    selected = random.sample(notes_list, min(5, len(notes_list)))
    notes_block = "\n\n".join(f"[Note {i+1}]\n{note[:4000]}" for i, note in enumerate(selected))
    prompt = QUIZ_PROMPT_TEMPLATE.format(n=len(selected), notes_block=notes_block)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    questions = json.loads(raw)
    for q in questions:
        q["book"] = clean_title(q.get("book", ""))
    if len(questions) != len(selected):
        raise ValueError(f"Expected {len(selected)} questions, got {len(questions)}")
    print(f"Generated {len(questions)} questions.")
    return questions


# ─── 3. Facts Generation ────────────────────────────────────────────────────

FACTS_PROMPT_TEMPLATE = """You are a curator of interesting ideas. Below are {n} separate notes, each from a different book. Extract exactly one surprising, insightful, or counterintuitive fact per note — {n} facts total.

Requirements:
- Each fact should be genuinely interesting — something that makes the reader think "I didn't know that" or "that's a great way to think about it"
- Include a short title (3–6 words) and a 1–3 sentence explanation
- Fact i must be drawn solely from Note i

Return ONLY a valid JSON array with this exact structure (no markdown, no commentary):
[
  {{
    "title": "...",
    "fact": "...",
    "book": "...",
    "author": "...",
    "topic": "..."
  }}
]

The "book" and "author" fields should reflect the source book for that note.
The "topic" field should be a short genre or subject label (2–4 words, e.g. "World War I", "Behavioural Economics", "Ancient Rome").

{notes_block}"""


def generate_facts(notes_list: list[str]) -> list[dict]:
    print("Generating interesting facts with Claude...")
    selected = random.sample(notes_list, min(5, len(notes_list)))
    notes_block = "\n\n".join(f"[Note {i+1}]\n{note[:4000]}" for i, note in enumerate(selected))
    prompt = FACTS_PROMPT_TEMPLATE.format(n=len(selected), notes_block=notes_block)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    facts = json.loads(raw)
    for f in facts:
        f["book"] = clean_title(f.get("book", ""))
    if len(facts) != len(selected):
        raise ValueError(f"Expected {len(selected)} facts, got {len(facts)}")
    print(f"Generated {len(facts)} facts.")
    return facts


# ─── 4. HTML Generation ─────────────────────────────────────────────────────

def generate_html(questions: list[dict], facts: list[dict], date_str: str) -> str:
    questions_json = json.dumps(questions, ensure_ascii=False)
    facts_json = json.dumps(facts, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Daily Quiz – {date_str}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
      background: #ffffff;
      min-height: 100vh;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      padding: 48px 24px;
      color: #0a0a0a;
    }}
    .card {{
      max-width: 600px;
      width: 100%;
    }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 32px;
    }}
    .header-left {{}}
    .header-label {{
      font-size: 0.78rem;
      font-weight: 600;
      color: #999999;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 4px;
    }}
    .header-date {{
      font-size: 1.1rem;
      font-weight: 600;
      color: #888888;
    }}
    .home-btn {{
      display: none;
      padding: 8px 18px;
      background: #ffffff;
      color: #0a0a0a;
      border: 1px solid #e8e8e8;
      border-radius: 999px;
      font-size: 0.82rem;
      font-family: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s;
    }}
    .home-btn:hover {{ border-color: #0a0a0a; background: #f5f5f5; }}
    .home-btn.visible {{ display: inline-block; }}
    .progress-bar {{
      height: 3px;
      background: #e8e8e8;
      border-radius: 999px;
      margin-bottom: 40px;
      overflow: hidden;
    }}
    .progress-bar-fill {{
      height: 100%;
      background: #00c87a;
      border-radius: 999px;
      transition: width 0.4s ease;
    }}

    /* ── Source attribution ── */
    .source-tag {{
      margin-bottom: 16px;
    }}
    .source-book {{
      font-size: 1rem;
      font-weight: 700;
      color: #0a0a0a;
      font-style: italic;
    }}
    .source-author {{
      font-size: 0.95rem;
      color: #555555;
      margin-left: 4px;
    }}
    .source-topic {{
      font-size: 0.78rem;
      font-weight: 600;
      color: #999999;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      margin-top: 4px;
    }}

    /* ── Home screen ── */
    .home-screen {{
      display: none;
    }}
    .home-screen.show {{ display: block; }}
    .home-title {{
      font-size: 2rem;
      font-weight: 700;
      line-height: 1.25;
      margin-bottom: 12px;
    }}
    .home-subtitle {{
      font-size: 0.95rem;
      color: #666666;
      line-height: 1.6;
      margin-bottom: 40px;
    }}
    .path-cards {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .path-card {{
      border: 1px solid #e8e8e8;
      border-radius: 16px;
      padding: 24px 28px;
      cursor: pointer;
      text-align: left;
      background: #ffffff;
      font-family: inherit;
      transition: border-color 0.15s, background 0.15s;
      width: 100%;
    }}
    .path-card:hover {{
      border-color: #0a0a0a;
      background: #fafafa;
    }}
    .path-card-title {{
      font-size: 1.05rem;
      font-weight: 700;
      margin-bottom: 6px;
      color: #0a0a0a;
    }}
    .path-card-desc {{
      font-size: 0.875rem;
      color: #666666;
      line-height: 1.55;
    }}
    .path-card-tag {{
      display: inline-block;
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      border-radius: 999px;
      padding: 3px 10px;
      margin-bottom: 10px;
    }}
    .tag-quiz {{ background: #e6fff4; color: #007a4a; }}
    .tag-facts {{ background: #f0f0ff; color: #4040cc; }}

    /* ── Archive ── */
    .archive-section {{
      margin-top: 48px;
      padding-top: 32px;
      border-top: 1px solid #e8e8e8;
    }}
    .archive-heading {{
      font-size: 0.78rem;
      font-weight: 600;
      color: #999999;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 16px;
    }}
    .archive-list {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .archive-item {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 0;
      border-bottom: 1px solid #f0f0f0;
      text-decoration: none;
      color: #0a0a0a;
      font-size: 0.95rem;
      transition: color 0.15s;
    }}
    .archive-item:hover {{ color: #00c87a; }}
    .archive-item-arrow {{ color: #cccccc; font-size: 0.85rem; }}

    /* ── Quiz ── */
    .quiz-screen {{ display: none; }}
    .quiz-screen.show {{ display: block; }}
    .question {{
      font-size: 1.5rem;
      font-weight: 700;
      line-height: 1.4;
      margin-bottom: 32px;
      color: #0a0a0a;
    }}
    .options {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-bottom: 32px;
    }}
    .option-btn {{
      text-align: left;
      padding: 16px 22px;
      border: 1px solid #e8e8e8;
      border-radius: 999px;
      background: #ffffff;
      font-size: 0.95rem;
      font-family: inherit;
      color: #0a0a0a;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s, color 0.15s;
      line-height: 1.45;
    }}
    .option-btn:hover:not(:disabled) {{ background: #f5f5f5; }}
    .option-btn.correct {{
      border-color: #00c87a;
      background: #e6fff4;
      color: #007a4a;
    }}
    .option-btn.wrong {{
      border-color: #ff4d4d;
      background: #fff0f0;
      color: #cc0000;
    }}
    .option-btn:disabled {{ cursor: default; }}
    .explanation {{
      display: none;
      border-left: 3px solid #e8e8e8;
      padding: 12px 16px;
      font-size: 0.875rem;
      color: #666666;
      margin-bottom: 28px;
      line-height: 1.6;
    }}
    .explanation.show {{ display: block; }}

    /* ── Score screen ── */
    .score-screen {{
      display: none;
      text-align: center;
      padding-top: 24px;
    }}
    .score-screen.show {{ display: block; }}
    .score-big {{
      font-size: 5rem;
      font-weight: 700;
      color: #0a0a0a;
      line-height: 1;
      margin-bottom: 8px;
    }}
    .score-label {{
      font-size: 1rem;
      color: #999999;
      margin-bottom: 24px;
    }}
    .score-msg {{
      font-size: 1.2rem;
      font-weight: 600;
      color: #0a0a0a;
      margin-bottom: 40px;
    }}

    /* ── Facts screen ── */
    .facts-screen {{ display: none; }}
    .facts-screen.show {{ display: block; }}
    .fact-number {{
      font-size: 0.78rem;
      font-weight: 600;
      color: #999999;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 20px;
    }}
    .fact-title {{
      font-size: 1.5rem;
      font-weight: 700;
      line-height: 1.35;
      margin-bottom: 16px;
      color: #0a0a0a;
    }}
    .fact-body {{
      font-size: 1rem;
      color: #444444;
      line-height: 1.7;
      margin-bottom: 40px;
    }}

    /* ── Facts done screen ── */
    .facts-done-screen {{
      display: none;
      text-align: center;
      padding-top: 24px;
    }}
    .facts-done-screen.show {{ display: block; }}
    .facts-done-title {{
      font-size: 2rem;
      font-weight: 700;
      margin-bottom: 12px;
    }}
    .facts-done-msg {{
      font-size: 1rem;
      color: #666666;
      margin-bottom: 40px;
      line-height: 1.6;
    }}

    /* ── Shared buttons ── */
    .btn-primary {{
      display: inline-block;
      padding: 14px 32px;
      background: #0a0a0a;
      color: #ffffff;
      border: none;
      border-radius: 999px;
      font-size: 0.95rem;
      font-family: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.15s;
    }}
    .btn-primary:hover {{ opacity: 0.82; }}
    .btn-outline {{
      display: inline-block;
      padding: 14px 32px;
      background: #ffffff;
      color: #0a0a0a;
      border: 1px solid #0a0a0a;
      border-radius: 999px;
      font-size: 0.95rem;
      font-family: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s, color 0.15s;
    }}
    .btn-outline:hover {{ background: #0a0a0a; color: #ffffff; }}
    .btn-hidden {{ display: none; }}
    .btn-row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
  </style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="header-left">
      <div class="header-label">Daily Learning</div>
      <div class="header-date">{date_str}</div>
    </div>
    <button class="home-btn" id="home-btn" onclick="showHome()">← Home</button>
  </div>
  <div class="progress-bar"><div class="progress-bar-fill" id="progress-fill" style="width:0%"></div></div>

  <!-- Home screen -->
  <div class="home-screen show" id="home-screen">
    <div class="home-title">Daily Insights from Hyder's Favourite Reads</div>
    <div class="home-subtitle">
      Every book I read, I take notes. This is where those notes come to life — a daily quiz or a fresh insight, refreshed every morning.
    </div>
    <div class="path-cards">
      <button class="path-card" onclick="startFacts()">
        <div class="path-card-tag tag-facts">Interesting Facts</div>
        <div class="path-card-title">Just learn something new</div>
        <div class="path-card-desc">5 surprising or thought-provoking ideas pulled from today's reading. No right or wrong — just things worth knowing.</div>
      </button>
      <button class="path-card" onclick="startQuiz()">
        <div class="path-card-tag tag-quiz">Quiz</div>
        <div class="path-card-title">Test your knowledge</div>
        <div class="path-card-desc">5 multiple-choice questions on ideas from your books. See how much stuck — answers and explanations included.</div>
      </button>
    </div>
    <div class="archive-section" id="archive-section" style="display:none;">
      <div class="archive-heading">Past Quizzes</div>
      <div class="archive-list" id="archive-list"></div>
    </div>
  </div>

  <!-- Quiz screen -->
  <div class="quiz-screen" id="quiz-screen">
    <div class="source-tag" id="quiz-source"></div>
    <div class="question" id="question-text"></div>
    <div class="options" id="options-container"></div>
    <div class="explanation" id="explanation"></div>
    <div class="btn-row">
      <button class="btn-outline btn-hidden" id="back-btn">← Back</button>
      <button class="btn-primary btn-hidden" id="next-btn">Next question →</button>
    </div>
  </div>

  <!-- Score screen -->
  <div class="score-screen" id="score-screen">
    <div class="score-big" id="score-value"></div>
    <div class="score-label">out of 5</div>
    <div class="score-msg" id="score-msg"></div>
    <div class="btn-row" style="justify-content:center; gap:12px;">
      <button class="btn-outline" onclick="showHome()">Back to home</button>
      <button class="btn-outline" onclick="startQuiz()">Try again</button>
    </div>
  </div>

  <!-- Facts screen -->
  <div class="facts-screen" id="facts-screen">
    <div class="fact-number" id="fact-number"></div>
    <div class="fact-title" id="fact-title"></div>
    <div class="fact-body" id="fact-body"></div>
    <div class="source-tag" id="fact-source"></div>
    <div class="btn-row">
      <button class="btn-outline btn-hidden" id="fact-back-btn">← Back</button>
      <button class="btn-primary" id="fact-next-btn">Next fact →</button>
    </div>
  </div>

  <!-- Facts done screen -->
  <div class="facts-done-screen" id="facts-done-screen">
    <div class="facts-done-title">That's a wrap.</div>
    <div class="facts-done-msg">5 ideas from today's reading. Come back tomorrow for more.</div>
    <div class="btn-row" style="justify-content:center; gap:12px;">
      <button class="btn-outline" onclick="goBackFromFactsDone()">← Back</button>
      <button class="btn-outline" onclick="showHome()">Back to home</button>
      <button class="btn-outline" onclick="startFacts()">Read again</button>
    </div>
  </div>
</div>

<script>
const QUESTIONS = {questions_json};
const FACTS = {facts_json};

let current = 0;
let score = 0;
let answers = [];

function hide(id) {{ document.getElementById(id).classList.remove("show"); }}
function show(id) {{ document.getElementById(id).classList.add("show"); }}

function showHome() {{
  hide("quiz-screen");
  hide("score-screen");
  hide("facts-screen");
  hide("facts-done-screen");
  document.getElementById("progress-fill").style.width = "0%";
  document.getElementById("home-btn").classList.remove("visible");
  show("home-screen");
}}

// ── Quiz ──

function startQuiz() {{
  current = 0;
  score = 0;
  answers = [];
  hide("home-screen");
  hide("score-screen");
  document.getElementById("home-btn").classList.add("visible");
  show("quiz-screen");
  showQuestion();
}}

function showQuestion() {{
  const q = QUESTIONS[current];
  document.getElementById("progress-fill").style.width =
    `${{((current + 1) / QUESTIONS.length) * 100}}%`;
  document.getElementById("quiz-source").innerHTML =
    `<div><span class="source-book">${{q.book}}</span><span class="source-author">&mdash; ${{q.author}}</span></div><div class="source-topic">Topic: ${{q.topic}}</div>`;
  document.getElementById("question-text").textContent = q.question;

  const backBtn = document.getElementById("back-btn");
  if (current > 0) backBtn.classList.remove("btn-hidden");
  else backBtn.classList.add("btn-hidden");

  const nextBtn = document.getElementById("next-btn");
  const expEl = document.getElementById("explanation");
  const already = answers[current];
  const container = document.getElementById("options-container");
  container.innerHTML = "";
  const labels = ["A", "B", "C", "D"];

  if (already !== undefined) {{
    expEl.textContent = q.explanation;
    expEl.classList.add("show");
    nextBtn.classList.remove("btn-hidden");
    q.options.forEach((opt, i) => {{
      const btn = document.createElement("button");
      btn.className = "option-btn";
      btn.textContent = `${{labels[i]}}. ${{opt}}`;
      btn.disabled = true;
      if (i === q.answer) btn.classList.add("correct");
      else if (i === already) btn.classList.add("wrong");
      container.appendChild(btn);
    }});
  }} else {{
    expEl.textContent = "";
    expEl.classList.remove("show");
    nextBtn.classList.add("btn-hidden");
    q.options.forEach((opt, i) => {{
      const btn = document.createElement("button");
      btn.className = "option-btn";
      btn.textContent = `${{labels[i]}}. ${{opt}}`;
      btn.onclick = () => selectAnswer(i);
      container.appendChild(btn);
    }});
  }}
}}

function selectAnswer(chosen) {{
  answers[current] = chosen;
  const q = QUESTIONS[current];
  const buttons = document.querySelectorAll(".option-btn");
  buttons.forEach(b => b.disabled = true);
  if (chosen === q.answer) {{
    buttons[chosen].classList.add("correct");
    score++;
  }} else {{
    buttons[chosen].classList.add("wrong");
    buttons[q.answer].classList.add("correct");
  }}
  const expEl = document.getElementById("explanation");
  expEl.textContent = q.explanation;
  expEl.classList.add("show");
  document.getElementById("next-btn").classList.remove("btn-hidden");
}}

document.getElementById("back-btn").addEventListener("click", () => {{
  current--;
  showQuestion();
}});

document.getElementById("next-btn").addEventListener("click", () => {{
  current++;
  if (current < QUESTIONS.length) {{
    showQuestion();
  }} else {{
    hide("quiz-screen");
    show("score-screen");
    document.getElementById("score-value").textContent = score;
    const msgs = [
      "Keep reading — you'll get there!",
      "Not bad, keep going!",
      "Good effort!",
      "Well done!",
      "Almost perfect!",
      "Perfect score! Outstanding!"
    ];
    document.getElementById("score-msg").textContent = msgs[score];
  }}
}});

// ── Facts ──

function startFacts() {{
  current = 0;
  hide("home-screen");
  hide("facts-done-screen");
  document.getElementById("home-btn").classList.add("visible");
  show("facts-screen");
  showFact();
}}

function showFact() {{
  const f = FACTS[current];
  document.getElementById("progress-fill").style.width =
    `${{((current + 1) / FACTS.length) * 100}}%`;
  document.getElementById("fact-number").textContent =
    `Fact ${{current + 1}} of ${{FACTS.length}}`;
  document.getElementById("fact-title").textContent = f.title;
  document.getElementById("fact-body").textContent = f.fact;
  document.getElementById("fact-source").innerHTML =
    `<div><span class="source-book">${{f.book}}</span><span class="source-author">&mdash; ${{f.author}}</span></div><div class="source-topic">Topic: ${{f.topic}}</div>`;
  document.getElementById("fact-next-btn").textContent =
    current < FACTS.length - 1 ? "Next fact →" : "Finish";
  const factBackBtn = document.getElementById("fact-back-btn");
  if (current > 0) factBackBtn.classList.remove("btn-hidden");
  else factBackBtn.classList.add("btn-hidden");
}}

document.getElementById("fact-back-btn").addEventListener("click", () => {{
  current--;
  showFact();
}});

document.getElementById("fact-next-btn").addEventListener("click", () => {{
  current++;
  if (current < FACTS.length) {{
    showFact();
  }} else {{
    hide("facts-screen");
    document.getElementById("progress-fill").style.width = "100%";
    show("facts-done-screen");
  }}
}});

function goBackFromFactsDone() {{
  current = FACTS.length - 1;
  hide("facts-done-screen");
  show("facts-screen");
  showFact();
}}

// ── Archive ──
fetch("archive.json")
  .then(r => r.json())
  .then(entries => {{
    if (!entries.length) return;
    const list = document.getElementById("archive-list");
    entries.forEach(e => {{
      const a = document.createElement("a");
      a.className = "archive-item";
      a.href = `archive/${{e.slug}}.html`;
      a.innerHTML = `<span>${{e.date}}</span><span class="archive-item-arrow">→</span>`;
      list.appendChild(a);
    }});
    document.getElementById("archive-section").style.display = "block";
  }})
  .catch(() => {{}});
</script>
</body>
</html>"""


# ─── 5. GitHub Pages Deployment ─────────────────────────────────────────────

def deploy_to_github(html: str, repo_path: str):
    print(f"Deploying to GitHub Pages at {repo_path}...")
    repo = Path(repo_path)
    if not repo.exists():
        raise FileNotFoundError(
            f"GitHub repo not found at {repo_path}. "
            "Please clone your GitHub Pages repo there first."
        )
    # Pull latest (skip if repo is empty / no remote branch yet)
    subprocess.run(["git", "-C", str(repo), "pull", "--rebase"], capture_output=True)

    today = datetime.date.today()
    date_slug = today.isoformat()                          # e.g. 2026-03-10
    date_label = today.strftime("%B %d, %Y")               # e.g. March 10, 2026

    # Write index.html (today's quiz)
    (repo / "index.html").write_text(html, encoding="utf-8")

    # Save a dated archive copy
    archive_dir = repo / "archive"
    archive_dir.mkdir(exist_ok=True)
    (archive_dir / f"{date_slug}.html").write_text(html, encoding="utf-8")

    # Update archive.json
    archive_json_path = repo / "archive.json"
    if archive_json_path.exists():
        entries = json.loads(archive_json_path.read_text(encoding="utf-8"))
    else:
        entries = []
    # Remove duplicate entry for today if re-running
    entries = [e for e in entries if e["slug"] != date_slug]
    entries.insert(0, {"date": date_label, "slug": date_slug})
    archive_json_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    # Commit and push
    subprocess.run(["git", "-C", str(repo), "add", "index.html",
                    f"archive/{date_slug}.html", "archive.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"Daily quiz {date_slug}"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "push"], check=True)
    print("Deployed successfully.")


# ─── 6. Email ────────────────────────────────────────────────────────────────

def send_email(quiz_url: str, date_str: str):
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]
    print(f"Sending email to {', '.join(recipients)}...")
    subject = "Daily quiz"
    body = f"""Hi!

Your daily quiz for {date_str} is ready:

{quiz_url}

5 questions based on your notes — takes about 2 minutes.

Good luck!
"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())
    print("Email sent.")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    date_str = datetime.date.today().strftime("%B %d, %Y")

    # 1. Extract notes
    raw_notes = extract_notes()
    notes_list = split_notes(raw_notes)
    print(f"Found {len(notes_list)} individual notes.")

    # 2. Generate quiz and facts
    questions = generate_quiz(notes_list)
    facts = generate_facts(notes_list)

    # 3. Generate HTML
    html = generate_html(questions, facts, date_str)

    # 4. Deploy
    deploy_to_github(html, GITHUB_REPO_PATH)

    # 5. Email
    if GITHUB_PAGES_URL:
        send_email(GITHUB_PAGES_URL, date_str)
    else:
        print("GITHUB_PAGES_URL not set — skipping email.")

    print("Done!")


if __name__ == "__main__":
    main()
