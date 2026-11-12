# TYPE WITH ASHIF — typing practice

TYPE WITH ASHIF is a keyboard typing practice app with a black and blue interface, live typing stats, mechanical key sounds, and account-backed result saving through a Flask API.

## 1. Requirements
- Python 3.10+
- VS Code
- MySQL 8+ only if you want MySQL; SQLite works by default.

## 2. Open in VS Code
Open the project folder in VS Code.

Create and activate a virtual environment:

### Windows PowerShell
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:
```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env`. Keep SQLite for the first run.
To enable AI Coach Feedback, add your Gemini API key to `GEMINI_API_KEY` in `.env`. The default model is `gemini-3.8-flash`; you can override it with `GEMINI_MODEL`.
To run without `.env`, set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in the shell or IDE launch environment before starting Flask. The server reads that variable directly; keep the key out of source files.

Start:
```bash
python app.py
```

Open http://127.0.0.1:5000

If port 5000 is already in use locally, `python app.py` automatically chooses a free port and prints it in the startup log; open the printed address instead. Railway's `PORT` value is always used as provided.

If the frontend and Flask backend are hosted on separate domains, set
`SOCKETIO_CORS_ALLOWED_ORIGINS` in the backend environment to the exact
frontend origin or comma-separated origins (for example,
`https://your-site.netlify.app`). The default allowlist covers local Flask
origins only.

## 3. Use MySQL instead
1. Create a MySQL database by running `schema.sql` in MySQL Workbench.
2. Install the dependencies above.
3. Change `DATABASE_URL` in `.env` to:
```text
mysql+pymysql://YOUR_USER:YOUR_PASSWORD@localhost/typeflow
```
URL-encode special characters in the password.

## 4. Current features
- Time, word-count, and technology passage typing tests with a black and blue visual theme.
- Mechanical key sounds with an on/off control and animated typing feedback.
- A responsive QWERTY virtual keyboard that highlights physical key presses.
- Live timer and WPM/accuracy display.
- Correct, incorrect, extra and missed character reporting.
- Keyboard restart shortcut.
- Login and registration with password hashing.
- Signed-in typing result persistence.
- AI Coach Feedback for WPM, accuracy, missed-character counts, and frequent missed keys.
- Personal history API and global leaderboard API.
- Responsive layout.

## 5. API routes
| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Typing application |
| GET | `/api/me` | Current session |
| POST | `/api/register` | Create account |
| POST | `/api/login` | Sign in |
| POST | `/api/logout` | Sign out |
| POST | `/api/results` | Save a result |
| POST | `/api/coach-feedback` or `/api/ai-feedback` | Generate AI typing feedback |
| GET | `/api/history` | Current user's history |
| GET | `/api/leaderboard?period=all` | Leaderboard |

## 6. Important production notes
This is a student-project starter, not a production deployment. Before public hosting, add CSRF protection to JSON POST routes, rate limiting, password reset email, database migrations, HTTPS, secure secret management, server-side test verification, and stronger automated tests. The current client reports test statistics to the server; a determined user can manipulate browser requests. The leaderboard is therefore suitable for a demo, not a high-stakes competition.
# type-with-ashif
