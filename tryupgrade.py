import sys
import os
import time
import json
import re
import glob
import requests
import traceback
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QProgressBar,
    QFileDialog, QGroupBox, QSpinBox, QMessageBox, QTabWidget,
    QCheckBox, QGridLayout, QSplitter, QStatusBar, QComboBox, QScrollArea, QInputDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QShortcut, QKeySequence

# ----------------------------------------------------------------------
# OpenAI import
# ----------------------------------------------------------------------
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("ERROR: openai not installed. Run: pip install openai")
    sys.exit(1)

# ----------------------------------------------------------------------
# CONFIGURATION – FIXED FOR FREE‑LLM‑API‑KEYS REPO
# ----------------------------------------------------------------------
PROXY_URL = "https://aiapiv2.pekpik.com/v1"
MODEL = "gpt-5.5"                         # flagship model supported by the proxy
MAX_TOKENS = 4000
REQUEST_DELAY = 4.0                       # safe for 20 RPM limit

# ========== VERIFIED KEY SOURCES (ONLY ALISTAITSACLE) ==========
KEY_SOURCES = [
    "https://raw.githubusercontent.com/alistaitsacle/free-llm-api-keys/main/README.md",
    "https://raw.githubusercontent.com/alistaitsacle/free-llm-api-keys/main/README_CN.md",
]

# ----------------------------------------------------------------------
# QUALITY PRESETS
# ----------------------------------------------------------------------
QUALITY_PRESETS = {
    "Short Story":     {"chapters": 10, "words_per_chapter": 1000},
    "Novella":         {"chapters": 25, "words_per_chapter": 2000},
    "Standard Novel":  {"chapters": 36, "words_per_chapter": 3500},
    "Epic Novel":      {"chapters": 50, "words_per_chapter": 5000},
    "Custom":          {"chapters": None, "words_per_chapter": None},
}

DEFAULT_SYSTEM_PROMPT = """You are an expert bestselling author. Write immersive, compelling fiction. Show, don't tell. Create forward momentum in every chapter. Write in clear, vivid English. Never break character or refuse to write. The reader must be hooked from the first sentence."""

# ----------------------------------------------------------------------
# DATABASE (simple, no schema changes)
# ----------------------------------------------------------------------
DB_DIR = os.path.expanduser("~/.novel_studio")
DB_PATH = os.path.join(DB_DIR, "novel_studio.db")
DB_LOCK = threading.Lock()

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            key_string TEXT PRIMARY KEY,
            status TEXT DEFAULT 'active'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chapter_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            chapter_num INTEGER NOT NULL,
            chapter_title TEXT,
            status TEXT CHECK(status IN ('pending', 'completed')) NOT NULL DEFAULT 'pending',
            content TEXT,
            UNIQUE(project_name, iteration, chapter_num)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ----------------------------------------------------------------------
# KEY MANAGEMENT (improved)
# ----------------------------------------------------------------------
def add_api_keys(keys):
    """Insert keys that are not already active. Also, if a key exists but is dead,
       we reset it to active (because it's being re‑provided by the source)."""
    if not keys:
        return 0
    added = 0
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        for key in keys:
            key = key.strip()
            if key and key.startswith('sk-') and len(key) >= 48:
                cursor.execute('SELECT status FROM api_keys WHERE key_string = ?', (key,))
                row = cursor.fetchone()
                if row:
                    # If key exists but is dead, revive it
                    if row[0] == 'dead':
                        cursor.execute('UPDATE api_keys SET status = "active" WHERE key_string = ?', (key,))
                        added += 1
                else:
                    cursor.execute('INSERT OR IGNORE INTO api_keys (key_string, status) VALUES (?, "active")', (key,))
                    if cursor.rowcount > 0:
                        added += 1
        conn.commit()
        conn.close()
    return added

def get_active_key():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute('SELECT key_string FROM api_keys WHERE status = "active" LIMIT 1')
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def mark_key_dead(key):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('UPDATE api_keys SET status = "dead" WHERE key_string = ?', (key,))
        conn.commit()
        conn.close()

def get_key_count():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM api_keys WHERE status = "active"')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def fetch_keys_from_sources():
    """Scrape ONLY valid keys from alistaitsacle repo (verified source)."""
    all_keys = []
    # Strict regex: sk- followed by exactly 48+ alphanumeric/underscore/hyphen
    key_regex = re.compile(r'(?:^|\s|`)(sk-[A-Za-z0-9_-]{47,})(?:\s|$|`)')
    
    for url in KEY_SOURCES:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                continue
            text = resp.text
            keys = key_regex.findall(text)
            if keys:
                all_keys.extend(keys)
        except Exception:
            pass
    
    if all_keys:
        # Remove duplicates (preserving order)
        unique_keys = list(dict.fromkeys(all_keys))
        return add_api_keys(unique_keys)
    return 0

# ----------------------------------------------------------------------
# CHAPTER TASK HELPERS (unchanged)
# ----------------------------------------------------------------------
def get_pending_tasks(project_name, iteration):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT chapter_num, chapter_title, content
        FROM chapter_tasks
        WHERE project_name = ? AND iteration = ? AND status = 'pending'
        ORDER BY chapter_num
    ''', (project_name, iteration))
    tasks = cursor.fetchall()
    conn.close()
    return [(row[0], row[1], row[2]) for row in tasks]

def create_tasks(project_name, iteration, chapter_titles):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        for num, title in enumerate(chapter_titles, 1):
            cursor.execute('''
                INSERT OR IGNORE INTO chapter_tasks (project_name, iteration, chapter_num, chapter_title, status)
                VALUES (?, ?, ?, ?, 'pending')
            ''', (project_name, iteration, num, title))
        conn.commit()
        conn.close()

def mark_task_completed(project_name, iteration, chapter_num, content):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE chapter_tasks
            SET status = 'completed', content = ?
            WHERE project_name = ? AND iteration = ? AND chapter_num = ?
        ''', (content, project_name, iteration, chapter_num))
        conn.commit()
        conn.close()

def get_task_content(project_name, iteration, chapter_num):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT content FROM chapter_tasks
        WHERE project_name = ? AND iteration = ? AND chapter_num = ?
    ''', (project_name, iteration, chapter_num))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def update_task_content(project_name, iteration, chapter_num, new_content):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE chapter_tasks
            SET content = ?, status = 'completed'
            WHERE project_name = ? AND iteration = ? AND chapter_num = ?
        ''', (new_content, project_name, iteration, chapter_num))
        conn.commit()
        conn.close()

# ----------------------------------------------------------------------
# WORKER (key rotation + rate limit handling)
# ----------------------------------------------------------------------
class GenerationWorker(QThread):
    log_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal()
    stream_signal = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.is_running = True

    def run(self):
        try:
            if self.config.get('mode') == 'single':
                self.rewrite_chapter()
            else:
                self.generate_book()
        except Exception as e:
            self.log_signal.emit(f"Critical Error: {str(e)}\n{traceback.format_exc()}", "error")
        finally:
            self.finished_signal.emit()

    def stop(self):
        self.is_running = False

    def _make_api_call(self, system_prompt, user_prompt):
        """Try keys in rotation. On 401/403 -> mark dead. On 429 -> sleep 15s and retry."""
        attempt = 0
        max_retries = 50
        
        while self.is_running and attempt < max_retries:
            attempt += 1
            key = get_active_key()
            if not key:
                self.log_signal.emit("🔄 No keys available. Waiting 30s for new keys...", "warning")
                fetch_keys_from_sources()
                for _ in range(30):
                    if not self.is_running:
                        return False, ""
                    time.sleep(1)
                continue

            try:
                self.log_signal.emit(f"   [Key ...{key[-8:]}] Requesting API", "info")
                client = OpenAI(api_key=key, base_url=PROXY_URL, timeout=30.0)
                is_streaming = self.config.get('stream_mode', False) and not self.config.get('parallel_mode', False)

                response = client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.9,
                    max_tokens=MAX_TOKENS,
                    stream=is_streaming
                )

                if is_streaming:
                    full_text = ""
                    for chunk in response:
                        if not self.is_running:
                            return False, ""
                        content = chunk.choices[0].delta.content
                        if content:
                            full_text += content
                            self.stream_signal.emit(content)
                    self.stream_signal.emit("\n\n---\n\n")
                    result = full_text
                else:
                    result = response.choices[0].message.content

                time.sleep(REQUEST_DELAY)
                return True, result

            except Exception as e:
                err = str(e).lower()
                if "401" in err or "403" in err or "invalid" in err or "unauthorized" in err:
                    self.log_signal.emit(f"   ❌ Key invalid – marking dead.", "error")
                    mark_key_dead(key)
                elif "429" in err or "rate" in err:
                    self.log_signal.emit(f"   ⚠️ Rate limit reached. Pausing 15s...", "warning")
                    time.sleep(15)
                else:
                    self.log_signal.emit(f"   ⚠️ Error: {str(e)[:100]} – rotating key.", "warning")
                    time.sleep(2)

        return False, ""

    # ------------------------------------------------------------------
    # Helper: get previous chapter ending
    # ------------------------------------------------------------------
    def get_previous_chapter_ending(self, project_name, iteration, current_ch_num):
        if current_ch_num <= 1:
            return None
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT content FROM chapter_tasks
            WHERE project_name = ? AND iteration = ? AND chapter_num = ? AND status = 'completed'
        ''', (project_name, iteration, current_ch_num - 1))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            words = row[0].split()
            return " ".join(words[-400:])
        return None

    # ------------------------------------------------------------------
    # Rewrite single chapter
    # ------------------------------------------------------------------
    def rewrite_chapter(self):
        project_name = self.clean_filename(self.config.get('book_title', 'Rewrite'))
        iteration = self.config.get('iterations', 1)
        chapter_num = self.config.get('rewrite_ch')
        target_words = self.config.get('rewrite_words', 3500)
        instructions = self.config.get('rewrite_prompt', '')

        old_content = get_task_content(project_name, iteration, chapter_num)
        if old_content is None:
            self.log_signal.emit(f"❌ Chapter {chapter_num} not found.", "error")
            return

        system_prompt = self.config['system_prompt']
        book_type = self.config['book_type']
        book_genre = self.config['book_genre']
        full_system = f"Context: You are an expert author writing a {book_type} book in the {book_genre} genre.\n\n{system_prompt}"

        user_prompt = f"""Rewrite chapter {chapter_num} according to:

INSTRUCTIONS: {instructions}
TARGET LENGTH: {target_words} words.

ORIGINAL:
{old_content}

Write the new version directly."""

        self.log_signal.emit(f"✏️ Rewriting Chapter {chapter_num}...", "info")
        success, new_content = self._make_api_call(full_system, user_prompt)
        if not success:
            self.log_signal.emit(f"❌ Failed rewrite.", "error")
            return

        update_task_content(project_name, iteration, chapter_num, new_content)

        base_output_dir = self.config['output_dir']
        iter_folder = os.path.join(base_output_dir, f"{project_name}_iter{iteration}")
        os.makedirs(iter_folder, exist_ok=True)

        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT chapter_title FROM chapter_tasks WHERE project_name = ? AND iteration = ? AND chapter_num = ?',
                       (project_name, iteration, chapter_num))
        row = cursor.fetchone()
        conn.close()
        chapter_title = row[0] if row else f"Chapter {chapter_num}"

        safe_title = self.clean_filename(chapter_title)
        filename = os.path.join(iter_folder, f"chapter_{chapter_num:02d}_{safe_title}.txt")
        with DB_LOCK:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"# Chapter {chapter_num}: {chapter_title}\n\n{new_content}")

        wc = len(new_content.split())
        self.log_signal.emit(f"✅ Rewritten and saved {filename} ({wc} words)", "success")

    # ------------------------------------------------------------------
    # Wrapper for parallel mode
    # ------------------------------------------------------------------
    def generate_single_chapter_wrapper(self, ch_num, ch_title, current_blueprint):
        if not self.is_running:
            return False

        target_words = self.config['target_words']
        final_system_prompt = self.config['final_system_prompt']
        blueprint_snippet = current_blueprint[:4000] if current_blueprint else ""
        user_prompt = f"Blueprint:\n{blueprint_snippet}\n\nWrite Chapter {ch_num}: {ch_title}. {target_words} words, present tense. Start directly."

        success, chapter_text = self._make_api_call(final_system_prompt, user_prompt)
        if not success:
            self.log_signal.emit(f"❌ Failed Chapter {ch_num}.", "error")
            return False

        with DB_LOCK:
            mark_task_completed(self.project_name, self.iteration, ch_num, chapter_text)
            safe_title = self.clean_filename(ch_title)
            filename = os.path.join(self.iter_folder, f"chapter_{ch_num:02d}_{safe_title}.txt")
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"# Chapter {ch_num}: {ch_title}\n\n{chapter_text}")
        self.log_signal.emit(f"✅ Chapter {ch_num} done.", "success")
        return True

    def clean_filename(self, name):
        clean = re.sub(r'[\\/*?:"<>|]', "", name).replace(" ", "_")
        return clean[:50].strip("_")

    def merge_chapters(self, folder, full_path, project_name, iteration):
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT chapter_num, chapter_title, content
            FROM chapter_tasks
            WHERE project_name = ? AND iteration = ? AND status = 'completed'
            ORDER BY chapter_num
        ''', (project_name, iteration))
        completed = cursor.fetchall()
        conn.close()

        if not completed:
            return 0

        with open(full_path, "w", encoding="utf-8") as outfile:
            for ch_num, ch_title, content in completed:
                outfile.write(f"# Chapter {ch_num}: {ch_title}\n\n{content}\n\n")

        md_path = full_path.replace(".txt", "_Formatted.md")
        clean_title = project_name.replace("_", " ")

        with open(md_path, "w", encoding="utf-8") as md:
            md.write(f"# {clean_title}\n\n---\n\n## Table of Contents\n\n")
            for ch_num, ch_title, _ in completed:
                anchor = f"chapter-{ch_num}-{ch_title.lower().replace(' ', '-')}"
                md.write(f"* [Chapter {ch_num}: {ch_title}](#{anchor})\n")
            md.write("\n---\n\n")
            for ch_num, ch_title, content in completed:
                anchor = f"chapter-{ch_num}-{ch_title.lower().replace(' ', '-')}"
                md.write(f"<a id='{anchor}'></a>\n## Chapter {ch_num}: {ch_title}\n\n")
                md.write(f"{content}\n\n---\n\n")

        self.log_signal.emit(f"✨ Markdown created: {os.path.basename(md_path)}", "success")
        return len(completed)

    def generate_book(self):
        original_blueprint_path = self.config['blueprint_path']
        base_output_dir = self.config['output_dir']
        total_iterations = self.config.get('iterations', 1)
        deepen = self.config.get('deepen', False)
        user_book_title = self.config.get('book_title', '').strip()
        parallel_mode = self.config.get('parallel_mode', False)
        max_workers = self.config.get('max_workers', 5)

        base_system_prompt = self.config['system_prompt']
        book_type = self.config['book_type']
        book_genre = self.config['book_genre']
        final_system_prompt = f"Context: You are an expert author writing a {book_type} book in the {book_genre} genre.\n\n{base_system_prompt}"

        if not os.path.exists(original_blueprint_path):
            self.log_signal.emit(f"❌ Blueprint not found.", "error")
            return
        with open(original_blueprint_path, "r", encoding="utf-8") as f:
            original_blueprint = f.read()

        book_title = user_book_title
        if not book_title:
            self.log_signal.emit("🧠 Generating title...", "info")
            title_prompt = f"Generate a compelling title for this {book_genre} book based on:\n\n{original_blueprint[:2000]}\n\nOutput only the title."
            success, title_text = self._make_api_call(final_system_prompt, title_prompt)
            if success and title_text:
                book_title = title_text.strip().replace('"', '').replace('**', '')[:60]
                self.log_signal.emit(f"📚 Title: {book_title}", "success")
            else:
                book_title = "Generated_Novel"
                self.log_signal.emit("⚠️ Using default title.", "warning")

        safe_title = self.clean_filename(book_title)
        if not safe_title:
            safe_title = "Novel"

        for iteration in range(1, total_iterations + 1):
            if not self.is_running:
                break

            self.log_signal.emit(f"\n{'='*60}\n🔄 ITERATION {iteration}/{total_iterations}\n{'='*60}", "info")

            if iteration == 1:
                current_blueprint = original_blueprint
            else:
                if deepen:
                    prev_folder = os.path.join(base_output_dir, f"{safe_title}_iter{iteration-1}")
                    prev_full = os.path.join(prev_folder, "full_novel.txt")
                    if os.path.exists(prev_full):
                        with open(prev_full, "r", encoding="utf-8") as f:
                            current_blueprint = f.read()
                        self.log_signal.emit("📖 Deepening: using previous novel as blueprint.", "info")
                    else:
                        self.log_signal.emit("⚠️ Previous novel not found.", "warning")
                        current_blueprint = original_blueprint
                else:
                    current_blueprint = original_blueprint

            iter_folder = os.path.join(base_output_dir, f"{safe_title}_iter{iteration}")
            os.makedirs(iter_folder, exist_ok=True)
            self.log_signal.emit(f"📁 Output: {iter_folder}", "info")

            total_chapters = self.config['total_chapters']
            target_words = self.config['target_words']

            conn = sqlite3.connect(DB_PATH, timeout=10)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM chapter_tasks WHERE project_name = ? AND iteration = ?', (safe_title, iteration))
            task_count = cursor.fetchone()[0]
            conn.close()

            if task_count == 0:
                self.log_signal.emit("🧠 Generating chapter titles...", "info")
                title_prompt = f"Generate exactly {total_chapters} chapter titles for:\n\n{current_blueprint[:4000]}\n\nNumbered list."
                success, meta_text = self._make_api_call(final_system_prompt, title_prompt)
                if not success:
                    self.log_signal.emit("❌ Using generic titles.", "error")
                    chapter_titles = [f"Chapter {i+1}" for i in range(total_chapters)]
                else:
                    lines = [line.strip() for line in meta_text.strip().split('\n') if line.strip()]
                    chapter_titles = []
                    for line in lines:
                        match = re.match(r"^\d+[\.)\-]?\s*(.+)", line)
                        if match:
                            chapter_titles.append(match.group(1).replace("**", "").strip())
                    if len(chapter_titles) < total_chapters:
                        chapter_titles += [f"Chapter {i+1}" for i in range(len(chapter_titles), total_chapters)]
                    elif len(chapter_titles) > total_chapters:
                        chapter_titles = chapter_titles[:total_chapters]

                create_tasks(safe_title, iteration, chapter_titles)
            else:
                conn = sqlite3.connect(DB_PATH, timeout=10)
                cursor = conn.cursor()
                cursor.execute('SELECT chapter_num, chapter_title FROM chapter_tasks WHERE project_name = ? AND iteration = ? ORDER BY chapter_num', (safe_title, iteration))
                rows = cursor.fetchall()
                chapter_titles = [title for _, title in rows]
                conn.close()

            pending = get_pending_tasks(safe_title, iteration)
            if not pending:
                self.log_signal.emit("✅ All chapters done!", "success")
                full_path = os.path.join(iter_folder, "full_novel.txt")
                cnt = self.merge_chapters(iter_folder, full_path, safe_title, iteration)
                self.log_signal.emit(f"📚 Merged {cnt} chapters.", "success")
                continue

            self.log_signal.emit(f"📝 {len(pending)} chapters pending.", "info")

            self.project_name = safe_title
            self.iteration = iteration
            self.iter_folder = iter_folder
            self.config['final_system_prompt'] = final_system_prompt

            if parallel_mode:
                self.log_signal.emit(f"⚡ Parallel mode (max {max_workers})", "info")
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_chapter = {
                        executor.submit(self.generate_single_chapter_wrapper, ch_num, ch_title, current_blueprint): ch_num
                        for ch_num, ch_title, _ in pending
                    }
                    completed_count = 0
                    for future in as_completed(future_to_chapter):
                        if not self.is_running:
                            executor.shutdown(wait=False)
                            break
                        ch_num = future_to_chapter[future]
                        try:
                            if future.result():
                                completed_count += 1
                                self.progress_signal.emit(int((completed_count / total_chapters) * 100))
                        except Exception as e:
                            self.log_signal.emit(f"❌ Thread error: {e}", "error")
            else:
                self.log_signal.emit("🔄 Sequential mode (with memory chaining)", "info")
                for ch_num, ch_title, _ in pending:
                    if not self.is_running:
                        break

                    prev_ending = self.get_previous_chapter_ending(safe_title, iteration, ch_num)
                    memory = ""
                    if prev_ending:
                        memory = f"\n\nCONTEXT (last 400 words of previous chapter):\n> \"{prev_ending}\"\nContinue smoothly from there.\n"

                    blueprint_snippet = current_blueprint[:4000] if current_blueprint else ""
                    user_prompt = f"Blueprint:\n{blueprint_snippet}{memory}\nNow write Chapter {ch_num}: {ch_title}. {target_words} words, present tense. Start directly."

                    success, chapter_text = self._make_api_call(final_system_prompt, user_prompt)
                    if not success:
                        self.log_signal.emit(f"❌ Failed Chapter {ch_num}. Retrying later.", "error")
                        continue

                    with DB_LOCK:
                        mark_task_completed(safe_title, iteration, ch_num, chapter_text)
                        safe_title_ch = self.clean_filename(ch_title)
                        filename = os.path.join(iter_folder, f"chapter_{ch_num:02d}_{safe_title_ch}.txt")
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write(f"# Chapter {ch_num}: {ch_title}\n\n{chapter_text}")
                    self.log_signal.emit(f"✅ Chapter {ch_num} saved.", "success")
                    self.progress_signal.emit(int((ch_num / total_chapters) * 100))
                    time.sleep(REQUEST_DELAY)

            full_path = os.path.join(iter_folder, "full_novel.txt")
            cnt = self.merge_chapters(iter_folder, full_path, safe_title, iteration)
            self.log_signal.emit(f"📚 Merged {cnt} chapters.", "success")

        if self.is_running:
            self.log_signal.emit(f"\n🎉 All done! Output: {os.path.abspath(base_output_dir)}", "success")
            self.progress_signal.emit(100)


# ============================================================================
# MAIN WINDOW
# ============================================================================
class NovelStudio(QMainWindow):
    CONFIG_DIR = os.path.expanduser("~/.novel_studio")
    CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Universal Novel Studio Pro")
        self.resize(1200, 850)
        self.setStyleSheet(self.get_stylesheet())

        self.genres = {
            "Fiction": ["Deep Psychological Thriller", "Thriller / Suspense", "Science Fiction", "Fantasy", "Romance", "Horror", "Mystery / Crime", "Historical Fiction", "Literary Fiction", "Young Adult (YA)"],
            "Non-Fiction": ["Self-Help / Personal Development", "Biography / Memoir", "Business & Economics", "True Crime", "History", "Philosophy", "Science & Technology", "Health & Wellness"]
        }

        self.worker = None
        self.bp_path = ""
        self.out_path = ""
        self.init_ui()
        self.load_settings()
        self.setup_shortcuts()
        self.update_status()

    def get_stylesheet(self):
        return """
            QMainWindow, QWidget { background-color: #1e1e2e; color: #cdd6f4; font-family: 'Segoe UI', 'Inter', sans-serif; }
            QGroupBox { border: 1px solid #313244; border-radius: 8px; margin-top: 1.5ex; font-weight: bold; color: #89b4fa; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; background-color: #1e1e2e; }
            QLabel { color: #cdd6f4; }
            QLineEdit, QTextEdit, QSpinBox, QComboBox { background-color: #313244; border: 1px solid #45475a; border-radius: 6px; padding: 6px; color: #cdd6f4; selection-background-color: #89b4fa; }
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #89b4fa; }
            QPushButton { background-color: #313244; border: none; border-radius: 6px; padding: 8px 14px; font-weight: bold; }
            QPushButton:hover { background-color: #45475a; }
            QPushButton#primary { background-color: #89b4fa; color: #1e1e2e; }
            QPushButton#primary:hover { background-color: #b4befe; }
            QPushButton#danger { background-color: #f38ba8; color: #1e1e2e; }
            QPushButton#danger:hover { background-color: #eba0ac; }
            QProgressBar { border: 1px solid #45475a; border-radius: 6px; text-align: center; background-color: #313244; }
            QProgressBar::chunk { background-color: #89b4fa; border-radius: 6px; }
            QTabWidget::pane { border: 1px solid #313244; background: #1e1e2e; border-radius: 8px; }
            QTabBar::tab { background: #313244; color: #cdd6f4; padding: 8px 20px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
            QTabBar::tab:selected { background: #45475a; color: #89b4fa; font-weight: bold; }
            QStatusBar { background-color: #313244; color: #cdd6f4; }
            QToolTip { background-color: #313244; color: #cdd6f4; border: 1px solid #89b4fa; }
        """

    def init_ui(self):
        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 12)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # LEFT PANEL (scrollable)
        left_panel = QWidget()
        left_main_layout = QVBoxLayout(left_panel)
        left_main_layout.setContentsMargins(0, 0, 0, 0)
        left_main_layout.setSpacing(8)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")
        scroll_content = QWidget()
        left_layout = QVBoxLayout(scroll_content)
        left_layout.setSpacing(12)

        # ----- Project Setup -----
        proj_group = QGroupBox("📁 Project Setup")
        proj_layout = QGridLayout()
        proj_layout.setVerticalSpacing(8)
        proj_layout.addWidget(QLabel("Book Title:"), 0, 0)
        self.book_title_input = QLineEdit()
        self.book_title_input.setPlaceholderText("Leave blank to auto‑generate")
        proj_layout.addWidget(self.book_title_input, 0, 1)

        proj_layout.addWidget(QLabel("Blueprint (.txt):"), 1, 0)
        bp_layout = QHBoxLayout()
        self.bp_label = QLabel("No file selected")
        self.bp_label.setStyleSheet("color: #a6adc8;")
        self.bp_btn = QPushButton("Browse")
        self.bp_btn.clicked.connect(self.select_blueprint)
        bp_layout.addWidget(self.bp_label, 1)
        bp_layout.addWidget(self.bp_btn)
        proj_layout.addLayout(bp_layout, 1, 1)

        proj_layout.addWidget(QLabel("Output Folder:"), 2, 0)
        out_layout = QHBoxLayout()
        self.out_label = QLabel("No folder selected")
        self.out_label.setStyleSheet("color: #a6adc8;")
        self.out_btn = QPushButton("Browse")
        self.out_btn.clicked.connect(self.select_output)
        out_layout.addWidget(self.out_label, 1)
        out_layout.addWidget(self.out_btn)
        proj_layout.addLayout(out_layout, 2, 1)

        proj_group.setLayout(proj_layout)
        left_layout.addWidget(proj_group)

        # ----- Quality & Genre -----
        quality_group = QGroupBox("⚙️ Quality & Genre")
        quality_layout = QGridLayout()
        quality_layout.setVerticalSpacing(8)
        quality_layout.addWidget(QLabel("Quality Preset:"), 0, 0)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITY_PRESETS.keys()))
        self.quality_combo.setCurrentText("Standard Novel")
        self.quality_combo.currentIndexChanged.connect(self.on_quality_changed)
        quality_layout.addWidget(self.quality_combo, 0, 1)

        quality_layout.addWidget(QLabel("Total Chapters:"), 1, 0)
        self.ch_spin = QSpinBox()
        self.ch_spin.setRange(1, 100)
        quality_layout.addWidget(self.ch_spin, 1, 1)

        quality_layout.addWidget(QLabel("Words / Chapter:"), 2, 0)
        self.wc_spin = QSpinBox()
        self.wc_spin.setRange(500, 10000)
        self.wc_spin.setSingleStep(100)
        quality_layout.addWidget(self.wc_spin, 2, 1)

        quality_layout.addWidget(QLabel("Book Type:"), 3, 0)
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Fiction", "Non-Fiction"])
        self.type_combo.currentIndexChanged.connect(self.update_genres)
        quality_layout.addWidget(self.type_combo, 3, 1)

        quality_layout.addWidget(QLabel("Genre:"), 4, 0)
        self.genre_combo = QComboBox()
        self.update_genres()
        quality_layout.addWidget(self.genre_combo, 4, 1)

        quality_group.setLayout(quality_layout)
        left_layout.addWidget(quality_group)

        # ----- Status + Refresh & Manual Key Buttons -----
        status_group = QGroupBox("📡 API Status")
        status_layout = QVBoxLayout()
        self.status_indicator = QLabel("🟡 Fetching keys...")
        self.status_indicator.setStyleSheet("font-size: 14px; font-weight: bold;")
        status_layout.addWidget(self.status_indicator)
        self.key_count_label = QLabel("Keys loaded: 0")
        status_layout.addWidget(self.key_count_label)

        refresh_btn = QPushButton("🔄 Refresh Keys (Auto-Fetch)")
        refresh_btn.clicked.connect(self.refresh_keys)
        status_layout.addWidget(refresh_btn)

        manual_btn = QPushButton("➕ Add Manual Key")
        manual_btn.clicked.connect(self.add_manual_key)
        status_layout.addWidget(manual_btn)

        status_group.setLayout(status_layout)
        left_layout.addWidget(status_group)

        # ----- Advanced Settings (collapsible) -----
        self.adv_group = QGroupBox("⚙️ Advanced Settings")
        self.adv_group.setCheckable(True)
        self.adv_group.setChecked(False)
        adv_layout = QVBoxLayout()

        iter_layout = QHBoxLayout()
        iter_layout.addWidget(QLabel("Iterations:"))
        self.iter_spin = QSpinBox()
        self.iter_spin.setRange(1, 5)
        self.iter_spin.setValue(1)
        iter_layout.addWidget(self.iter_spin)
        self.deepen_check = QCheckBox("Deepen story")
        iter_layout.addWidget(self.deepen_check)
        adv_layout.addLayout(iter_layout)

        self.parallel_mode_cb = QCheckBox("Enable Parallel Mode (faster)")
        adv_layout.addWidget(self.parallel_mode_cb)

        self.stream_mode_cb = QCheckBox("Enable Live Typing (requires Parallel OFF)")
        adv_layout.addWidget(self.stream_mode_cb)

        adv_layout.addWidget(QLabel("System Prompt:"))
        self.prompt_input = QTextEdit()
        self.prompt_input.setPlainText(DEFAULT_SYSTEM_PROMPT)
        self.prompt_input.setMinimumHeight(120)
        adv_layout.addWidget(self.prompt_input)

        self.adv_group.setLayout(adv_layout)
        left_layout.addWidget(self.adv_group)

        left_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        left_main_layout.addWidget(scroll_area)

        # ----- Control Buttons (fixed at bottom) -----
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("▶ Start Generation")
        self.start_btn.setObjectName("primary")
        self.start_btn.setFixedHeight(40)
        self.start_btn.clicked.connect(lambda: self.start_generation('full'))

        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setFixedHeight(40)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_generation)

        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)

        self.save_btn = QPushButton("💾 Save Settings")
        self.save_btn.clicked.connect(self.save_settings)

        left_main_layout.addLayout(btn_layout)
        left_main_layout.addWidget(self.save_btn)

        splitter.addWidget(left_panel)

        # RIGHT PANEL (Log and Live Viewer)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        self.tabs = QTabWidget()

        # Log tab (split between system log and live viewer)
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        self.progress_bar = QProgressBar()
        log_layout.addWidget(self.progress_bar)

        log_splitter = QSplitter(Qt.Orientation.Vertical)
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setStyleSheet("font-family: monospace; background-color: #11111b;")
        log_splitter.addWidget(self.console)

        self.live_viewer = QTextEdit()
        self.live_viewer.setReadOnly(True)
        self.live_viewer.setPlaceholderText("Live typing appears here...")
        self.live_viewer.setStyleSheet("font-family: 'Segoe UI'; font-size: 15px; line-height: 1.6; background-color: #1e1e2e; padding: 15px;")
        log_splitter.addWidget(self.live_viewer)

        log_layout.addWidget(log_splitter)
        self.tabs.addTab(log_tab, "📜 Generation Log")

        # Chapter Editor tab
        edit_tab = QWidget()
        edit_layout = QVBoxLayout(edit_tab)
        edit_layout.addWidget(QLabel("Chapter to Rewrite:"))
        self.rw_ch_spin = QSpinBox()
        self.rw_ch_spin.setRange(1, 100)
        edit_layout.addWidget(self.rw_ch_spin)
        edit_layout.addWidget(QLabel("Target Words:"))
        self.rw_wc_spin = QSpinBox()
        self.rw_wc_spin.setRange(500, 10000)
        self.rw_wc_spin.setValue(4000)
        edit_layout.addWidget(self.rw_wc_spin)
        edit_layout.addWidget(QLabel("Rewrite Instructions:"))
        self.rw_prompt = QTextEdit()
        self.rw_prompt.setPlaceholderText("e.g., Make it darker...")
        edit_layout.addWidget(self.rw_prompt)
        self.rw_start_btn = QPushButton("Rewrite Chapter")
        self.rw_start_btn.setObjectName("primary")
        self.rw_start_btn.clicked.connect(lambda: self.start_generation('single'))
        edit_layout.addWidget(self.rw_start_btn)
        self.tabs.addTab(edit_tab, "✏️ Chapter Editor")

        right_layout.addWidget(self.tabs)
        splitter.addWidget(right_panel)

        splitter.setSizes([500, 700])
        main_layout.addWidget(splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar_label = QLabel("Ready")
        self.status_bar.addWidget(self.status_bar_label)

        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    # UI Helpers
    # ------------------------------------------------------------------
    def on_quality_changed(self):
        preset = self.quality_combo.currentText()
        if preset in QUALITY_PRESETS and preset != "Custom":
            data = QUALITY_PRESETS[preset]
            if data["chapters"] is not None:
                self.ch_spin.setValue(data["chapters"])
            if data["words_per_chapter"] is not None:
                self.wc_spin.setValue(data["words_per_chapter"])

    def update_genres(self):
        current = self.type_combo.currentText()
        self.genre_combo.clear()
        self.genre_combo.addItems(self.genres[current])

    def update_status(self):
        count = get_key_count()
        self.key_count_label.setText(f"Keys loaded: {count}")
        if count > 0:
            self.status_indicator.setText("🟢 Ready")
            self.status_indicator.setStyleSheet("color: #a6e3a1; font-size: 14px; font-weight: bold;")
        else:
            self.status_indicator.setText("🔴 No Keys - Click Refresh")
            self.status_indicator.setStyleSheet("color: #f38ba8; font-size: 14px; font-weight: bold;")

    def refresh_keys(self):
        self.log("Fetching fresh API keys from GitHub...", "info")
        QApplication.processEvents()
        added = fetch_keys_from_sources()
        self.update_status()
        self.log(f"✅ Added {added} new keys. Total active: {get_key_count()}", "success")

    def add_manual_key(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "Add Manual Keys",
            "Paste sk-... keys (one per line):",
            ""
        )
        if ok and text.strip():
            keys = [k.strip() for k in text.splitlines() if k.strip() and k.startswith('sk-')]
            added = add_api_keys(keys)
            self.log(f"✅ Added {added} manual key(s)", "success")
            self.update_status()

    def select_blueprint(self):
        fname, _ = QFileDialog.getOpenFileName(self, 'Select Blueprint', '', 'Text Files (*.txt)')
        if fname:
            self.bp_path = fname
            self.bp_label.setText(os.path.basename(fname))

    def select_output(self):
        dname = QFileDialog.getExistingDirectory(self, 'Select Output Folder')
        if dname:
            self.out_path = dname
            self.out_label.setText(dname)

    def log(self, msg, typ="info"):
        colors = {"info": "#cdd6f4", "success": "#a6e3a1", "warning": "#f9e2af", "error": "#f38ba8"}
        color = colors.get(typ, "#cdd6f4")
        ts = time.strftime("%H:%M:%S")
        self.console.append(f"<span style='color:#6c7086;'>[{ts}]</span> <span style='color:{color};'>{msg}</span>")
        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def update_progress(self, val):
        self.progress_bar.setValue(val)

    def update_live_stream(self, text):
        cursor = self.live_viewer.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.live_viewer.setTextCursor(cursor)
        self.live_viewer.ensureCursorVisible()

    def setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+G"), self).activated.connect(lambda: self.start_generation('full'))
        QShortcut(QKeySequence("Ctrl+Shift+S"), self).activated.connect(self.stop_generation)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self.save_settings)

    def set_controls_enabled(self, enabled):
        self.book_title_input.setEnabled(enabled)
        self.bp_btn.setEnabled(enabled)
        self.out_btn.setEnabled(enabled)
        self.quality_combo.setEnabled(enabled)
        self.ch_spin.setEnabled(enabled)
        self.wc_spin.setEnabled(enabled)
        self.type_combo.setEnabled(enabled)
        self.genre_combo.setEnabled(enabled)
        self.adv_group.setEnabled(enabled)
        self.rw_ch_spin.setEnabled(enabled)
        self.rw_wc_spin.setEnabled(enabled)
        self.rw_prompt.setEnabled(enabled)
        self.rw_start_btn.setEnabled(enabled)
        self.save_btn.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------
    def load_settings(self):
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.book_title_input.setText(cfg.get("book_title", ""))
            self.quality_combo.setCurrentText(cfg.get("quality_preset", "Standard Novel"))
            self.ch_spin.setValue(cfg.get("total_chapters", 36))
            self.wc_spin.setValue(cfg.get("target_words_per_chapter", 3500))
            self.type_combo.setCurrentText(cfg.get("book_type", "Fiction"))
            idx = self.genre_combo.findText(cfg.get("book_genre", "Deep Psychological Thriller"))
            if idx >= 0:
                self.genre_combo.setCurrentIndex(idx)
            if "blueprint_path" in cfg and os.path.exists(cfg["blueprint_path"]):
                self.bp_path = cfg["blueprint_path"]
                self.bp_label.setText(os.path.basename(self.bp_path))
            if "output_dir" in cfg and os.path.exists(cfg["output_dir"]):
                self.out_path = cfg["output_dir"]
                self.out_label.setText(self.out_path)
            saved_prompt = cfg.get("system_prompt", "")
            if saved_prompt.strip():
                self.prompt_input.setPlainText(saved_prompt)
            else:
                self.prompt_input.setPlainText(DEFAULT_SYSTEM_PROMPT)

            self.iter_spin.setValue(cfg.get("iterations", 1))
            self.deepen_check.setChecked(cfg.get("deepen", False))
            self.parallel_mode_cb.setChecked(cfg.get("parallel_mode", False))
            self.stream_mode_cb.setChecked(cfg.get("stream_mode", False))

            self.log("Settings loaded.", "success")
            self.update_status()
        except Exception as e:
            self.log(f"Load error: {e}", "error")

    def save_settings(self):
        os.makedirs(self.CONFIG_DIR, exist_ok=True)
        cfg = {
            "book_title": self.book_title_input.text(),
            "quality_preset": self.quality_combo.currentText(),
            "total_chapters": self.ch_spin.value(),
            "target_words_per_chapter": self.wc_spin.value(),
            "book_type": self.type_combo.currentText(),
            "book_genre": self.genre_combo.currentText(),
            "blueprint_path": self.bp_path,
            "output_dir": self.out_path,
            "system_prompt": self.prompt_input.toPlainText(),
            "iterations": self.iter_spin.value(),
            "deepen": self.deepen_check.isChecked(),
            "parallel_mode": self.parallel_mode_cb.isChecked(),
            "stream_mode": self.stream_mode_cb.isChecked(),
        }
        try:
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4)
            self.log("Settings saved.", "success")
        except Exception as e:
            self.log(f"Save error: {e}", "error")

    def closeEvent(self, event):
        self.save_settings()
        event.accept()

    # ------------------------------------------------------------------
    # Generation control
    # ------------------------------------------------------------------
    def start_generation(self, mode):
        if not self.bp_path:
            QMessageBox.warning(self, "Missing Blueprint", "Select a blueprint file.")
            return
        if not self.out_path:
            QMessageBox.warning(self, "Missing Output Folder", "Select an output folder.")
            return

        self.save_settings()

        # Refresh keys before starting
        self.refresh_keys()

        if get_key_count() == 0:
            QMessageBox.warning(self, "No Keys", "No API keys available. Click 'Refresh Keys' and try again.")
            return

        config = {
            'mode': mode,
            'blueprint_path': self.bp_path,
            'output_dir': self.out_path,
            'book_type': self.type_combo.currentText(),
            'book_genre': self.genre_combo.currentText(),
            'system_prompt': self.prompt_input.toPlainText(),
            'iterations': self.iter_spin.value(),
            'deepen': self.deepen_check.isChecked(),
            'book_title': self.book_title_input.text().strip(),
            'total_chapters': self.ch_spin.value() if mode == 'full' else None,
            'target_words': self.wc_spin.value() if mode == 'full' else None,
            'rewrite_ch': self.rw_ch_spin.value() if mode == 'single' else None,
            'rewrite_words': self.rw_wc_spin.value() if mode == 'single' else None,
            'rewrite_prompt': self.rw_prompt.toPlainText() if mode == 'single' else None,
            'parallel_mode': self.parallel_mode_cb.isChecked(),
            'stream_mode': self.stream_mode_cb.isChecked(),
            'max_workers': 5,
        }

        if mode == 'single' and not config['rewrite_prompt']:
            QMessageBox.warning(self, "Missing Instructions", "Enter rewrite instructions.")
            return

        self.worker = GenerationWorker(config)
        self.set_controls_enabled(False)

        self.start_btn.setEnabled(False)
        self.rw_start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.console.clear()
        self.live_viewer.clear()

        self.worker.log_signal.connect(self.log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.stream_signal.connect(self.update_live_stream)
        self.worker.finished_signal.connect(self.on_generation_finished)
        self.worker.start()

    def stop_generation(self):
        if self.worker:
            self.log("Stopping...", "warning")
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.set_controls_enabled(True)

    def on_generation_finished(self):
        self.start_btn.setEnabled(True)
        self.rw_start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None
        self.set_controls_enabled(True)
        self.log("Generation finished.", "success")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
if __name__ == '__main__':
    # Initial key fetch
    print("🔑 Fetching fresh keys from verified sources...")
    added = fetch_keys_from_sources()
    print(f"✅ Ready with {get_key_count()} active keys")
    
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = NovelStudio()
    window.show()
    sys.exit(app.exec())
