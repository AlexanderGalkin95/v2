from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from flask_caching import Cache
from flask_compress import Compress
import sqlite3
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv, find_dotenv
import os
import shutil
import schedule
import time
import threading
import requests
import subprocess
import signal

app = Flask(__name__)
app.secret_key = os.urandom(24)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

cache = Cache(app, config={'CACHE_TYPE': 'simple'})
Compress(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
MAX_FILE_SIZE = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {'.pdf', '.jpeg', '.jpg', '.png', '.txt', '.doc', '.docx'}

# Глобальная переменная для процесса бота
bot_process = None

# Путь к интерпретатору в виртуальном окружении
PYTHON_PATH = os.path.join(os.getcwd(), 'venv', 'Scripts', 'python.exe') if os.name == 'nt' else os.path.join(os.getcwd(), 'venv', 'bin', 'python')

# Загрузка начальных настроек
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
EMPLOYEE_CHAT_ID = os.getenv("EMPLOYEE_CHAT_ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", generate_password_hash("admin"))
REMINDER_TIME = os.getenv("REMINDER_TIME", "09:00")
REMINDER_COUNT = int(os.getenv("REMINDER_COUNT", 1))
REMINDER_DAYS = int(os.getenv("REMINDER_DAYS", 1))

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

def send_telegram_message(chat_id, text, file_path=None, token=TELEGRAM_TOKEN):
    if file_path:
        file_url = f"https://api.telegram.org/bot{token}/sendDocument"
        with open(file_path, 'rb') as file:
            response = requests.post(file_url, data={'chat_id': chat_id, 'caption': text}, files={'document': file})
    else:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        response = requests.post(url, data={'chat_id': chat_id, 'text': text})
    if response.status_code != 200:
        print(f"Ошибка Telegram: {response.text}")

def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS requests
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_date TEXT, company TEXT, name TEXT, address TEXT, 
                  contact_number TEXT, track_number TEXT, status TEXT, chat_id TEXT, received INTEGER DEFAULT 0, 
                  attachment TEXT)''')
    c.execute("ALTER TABLE requests ADD COLUMN attachment TEXT" if 'attachment' not in [col[1] for col in c.execute("PRAGMA table_info(requests)")] else "SELECT 1")
    conn.commit()
    conn.close()

def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def clean_old_files():
    now = datetime.now()
    cutoff = now - timedelta(days=7)
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.isfile(file_path) and datetime.fromtimestamp(os.path.getmtime(file_path)) < cutoff:
            os.remove(file_path)

def run_cleanup_scheduler():
    schedule.every().day.at("00:00").do(clean_old_files)
    while True:
        schedule.run_pending()
        time.sleep(60)

def update_env(key, value):
    env_file = find_dotenv()
    with open(env_file, 'r') as file:
        lines = file.readlines()
    with open(env_file, 'w') as file:
        found = False
        for line in lines:
            if line.startswith(f"{key}="):
                file.write(f"{key}={value}\n")
                found = True
            else:
                file.write(line)
        if not found:
            file.write(f"{key}={value}\n")
    os.environ[key] = value

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            login_user(User(username))
            return redirect(url_for('index'))
        flash('Пожалуйста, введите логин и пароль.')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/', methods=['GET'])
@login_required
def index():
    init_db()
    ITEMS_PER_PAGE = 10
    page = int(request.args.get('page', 1))
    offset = (page - 1) * ITEMS_PER_PAGE
    search = request.args.get('search', '')
    status = request.args.get('status', '')

    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    count_query = "SELECT COUNT(*) FROM requests WHERE 1=1"
    count_params = []
    if search:
        count_query += " AND (company LIKE ? OR name LIKE ? OR track_number LIKE ?)"
        count_params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    if status:
        count_query += " AND status = ?"
        count_params.append(status)
    c.execute(count_query, count_params)
    total_items = c.fetchone()[0]
    total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    query = "SELECT id, submission_date, company, name, address, contact_number, track_number, status, attachment FROM requests WHERE 1=1"
    params = []
    if search:
        query += " AND (company LIKE ? OR name LIKE ? OR track_number LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " LIMIT ? OFFSET ?"
    params.extend([ITEMS_PER_PAGE, offset])
    c.execute(query, params)
    requests_list = c.fetchall()

    c.execute("SELECT COUNT(*) FROM requests WHERE status != 'Отправлено'")
    overdue_count = c.fetchone()[0]
    conn.close()

    requests_json = [{'id': r[0], 'submission_date': r[1], 'company': r[2], 'name': r[3], 'address': r[4],
                      'contact_number': r[5], 'track_number': r[6], 'status': r[7], 'attachment': r[8]} for r in requests_list]

    return render_template('index.html', requests=requests_json, overdue_count=overdue_count, page=page, total_pages=total_pages,
                           search=search, status=status)

@app.route('/update_status', methods=['POST'])
@login_required
@cache.cached(timeout=10)
def update_status():
    request_id = request.form['request_id']
    action = request.form.get('action')
    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    if action == 'delete':
        c.execute("DELETE FROM requests WHERE id = ?", (request_id,))
    elif action == 'status':
        new_status = request.form['status']
        if new_status == 'Отправлено':
            track_number = request.form.get('track_number', '')
            attachment = request.files.get('attachment')
            if not track_number:
                conn.close()
                return jsonify({'success': False, 'message': 'Трек-номер обязателен для "Отправлено"'})
            if attachment and allowed_file(attachment.filename) and attachment.content_length <= MAX_FILE_SIZE:
                filename = f"{request_id}_{attachment.filename}"
                attachment.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                c.execute("UPDATE requests SET status = ?, track_number = ?, attachment = ? WHERE id = ?",
                          (new_status, track_number, filename, request_id))
            else:
                c.execute("UPDATE requests SET status = ?, track_number = ? WHERE id = ?",
                          (new_status, track_number, request_id))
        else:
            c.execute("UPDATE requests SET status = ?, track_number = NULL, attachment = NULL WHERE id = ?",
                      (new_status, request_id))
        c.execute("SELECT company, chat_id FROM requests WHERE id = ?", (request_id,))
        company, chat_id = c.fetchone()
        message = f"📋 Заявка {company}: статус изменён на '{new_status}'" + (f", трек: {track_number}" if new_status == 'Отправлено' else "")
        send_telegram_message(chat_id, message, os.path.join(app.config['UPLOAD_FOLDER'], filename) if 'filename' in locals() else None)

    conn.commit()
    conn.close()
    cache.clear()
    return filter_requests()

@app.route('/filter_requests', methods=['POST'])
@login_required
@cache.cached(timeout=60, key_prefix=lambda: f"filter_{request.form.get('search', '')}_{request.form.get('status', '')}_{request.args.get('page', '1')}")
def filter_requests():
    ITEMS_PER_PAGE = 10
    page = int(request.args.get('page', 1))
    search = request.form.get('search', '')
    status = request.form.get('status', '')

    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    count_query = "SELECT COUNT(*) FROM requests WHERE 1=1"
    count_params = []
    if search:
        count_query += " AND (company LIKE ? OR name LIKE ? OR track_number LIKE ?)"
        count_params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    if status:
        count_query += " AND status = ?"
        count_params.append(status)
    c.execute(count_query, count_params)
    total_items = c.fetchone()[0]
    total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    if page > total_pages:
        page = max(1, total_pages)
    offset = (page - 1) * ITEMS_PER_PAGE

    query = "SELECT id, submission_date, company, name, address, contact_number, track_number, status, attachment FROM requests WHERE 1=1"
    params = []
    if search:
        query += " AND (company LIKE ? OR name LIKE ? OR track_number LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " LIMIT ? OFFSET ?"
    params.extend([ITEMS_PER_PAGE, offset])
    c.execute(query, params)
    requests_list = c.fetchall()

    c.execute("SELECT COUNT(*) FROM requests WHERE status != 'Отправлено'")
    overdue_count = c.fetchone()[0]
    conn.close()

    requests_json = [{'id': r[0], 'submission_date': r[1], 'company': r[2], 'name': r[3], 'address': r[4],
                      'contact_number': r[5], 'track_number': r[6], 'status': r[7], 'attachment': r[8]} for r in requests_list]

    return jsonify({
        'success': True,
        'requests': requests_json,
        'overdue_count': overdue_count,
        'page': page,
        'total_pages': total_pages,
        'search': search,
        'status': status
    })

@app.route('/analytics')
@login_required
@cache.cached(timeout=300)
def analytics():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM requests GROUP BY status")
    stats = c.fetchall()
    conn.close()
    return render_template('analytics.html', stats=stats)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    global bot_process, TELEGRAM_TOKEN, EMPLOYEE_CHAT_ID, REMINDER_TIME, REMINDER_COUNT, REMINDER_DAYS, ADMIN_USERNAME, ADMIN_PASSWORD_HASH

    if request.method == 'POST':
        # Обновление логина и пароля
        new_username = request.form.get('username')
        new_password = request.form.get('password')
        if new_username and new_password:
            ADMIN_USERNAME = new_username
            ADMIN_PASSWORD_HASH = generate_password_hash(new_password)
            update_env('ADMIN_USERNAME', new_username)
            update_env('ADMIN_PASSWORD_HASH', ADMIN_PASSWORD_HASH)
            flash('Логин и пароль успешно обновлены')

        # Обновление настроек бота
        telegram_token = request.form.get('telegram_token')
        employee_chat_id = request.form.get('employee_chat_id')
        reminder_time = request.form.get('reminder_time')
        reminder_count = request.form.get('reminder_count')
        reminder_days = request.form.get('reminder_days')

        if telegram_token:
            TELEGRAM_TOKEN = telegram_token
            update_env('TELEGRAM_TOKEN', telegram_token)
        if employee_chat_id:
            EMPLOYEE_CHAT_ID = employee_chat_id
            update_env('EMPLOYEE_CHAT_ID', employee_chat_id)
        if reminder_time:
            REMINDER_TIME = reminder_time
            update_env('REMINDER_TIME', reminder_time)
        if reminder_count:
            REMINDER_COUNT = int(reminder_count)
            update_env('REMINDER_COUNT', str(reminder_count))
        if reminder_days:
            REMINDER_DAYS = int(reminder_days)
            update_env('REMINDER_DAYS', str(reminder_days))

        action = request.form.get('action')
        if action == 'start_bot':
            if not bot_process or bot_process.poll() is not None:
                bot_process = subprocess.Popen([PYTHON_PATH, 'bot.py'])
                flash('Бот запущен')
            else:
                flash('Бот уже работает')
        elif action == 'restart_bot':
            if bot_process and bot_process.poll() is None:
                bot_process.send_signal(signal.SIGTERM)
                bot_process.wait()
            bot_process = subprocess.Popen([PYTHON_PATH, 'bot.py'])
            flash('Бот перезапущен')
        elif action == 'stop_bot':
            if bot_process and bot_process.poll() is None:
                bot_process.send_signal(signal.SIGTERM)
                bot_process.wait()
                bot_process = None
                flash('Бот остановлен')
            else:
                flash('Бот уже остановлен')

        return redirect(url_for('settings'))

    bot_running = bot_process is not None and bot_process.poll() is None
    return render_template('settings.html', telegram_token=TELEGRAM_TOKEN, employee_chat_id=EMPLOYEE_CHAT_ID,
                           reminder_time=REMINDER_TIME, reminder_count=REMINDER_COUNT, reminder_days=REMINDER_DAYS,
                           bot_running=bot_running)

if __name__ == '__main__':
    threading.Thread(target=run_cleanup_scheduler, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5000)