from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from html import escape
from http.cookies import SimpleCookie
import secrets
import os
import time

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

BASE_DIR = Path(__file__).resolve().parent

# Results PIN: set your own with the RESULTS_PIN environment variable
DEFAULT_PIN = "1234"
RESULTS_PIN = os.environ.get("RESULTS_PIN", DEFAULT_PIN)

MAX_BODY = 10_000          # largest form body we accept (bytes)
MAX_PIN_ATTEMPTS = 5       # wrong PINs allowed ...
LOCK_SECONDS = 60          # ... before that client is locked out

candidates = [
    "Aarav Sharma",
    "Priya Verma",
    "Rahul Patil",
    "NOTA"
]

symbols = ["🧑‍💼", "👩‍💼", "🧑‍🎓", "🗳️"]

votes = [0] * len(candidates)
voted_users = set()
valid_result_tokens = set()
pin_attempts = {}          # client ip -> [failed_count, locked_until]

# Fixed messages. The URL carries only a short code, so nobody can put
# their own text on the page by crafting a link.
DEFAULT_MESSAGE = "Welcome to VoteSphere. Select a candidate and cast your vote."
MESSAGES = {
    "ok": "Your vote was submitted successfully!",
    "bad_id": "Invalid Voter ID. Enter 1 to 12 digits only.",
    "dup": "This Voter ID has already voted.",
    "bad_choice": "Please select a valid candidate.",
}


def pin_matches(supplied, expected):
    # compare_digest raises TypeError for non-ASCII str, so compare bytes
    return secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


class VotingServer(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))

    def send_html(self, content, status=200, extra_headers=None):
        data = content.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")

        if extra_headers:
            for key, value in extra_headers:
                self.send_header(key, value)

        self.end_headers()
        self.wfile.write(data)

    def read_form(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0

        length = max(0, min(length, MAX_BODY))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw, keep_blank_values=True)

    def redirect(self, location, headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")

        if headers:
            for key, value in headers:
                self.send_header(key, value)

        self.send_header("Content-Length", "0")
        self.end_headers()

    def get_result_token(self):
        cookie = SimpleCookie()

        try:
            cookie.load(self.headers.get("Cookie", ""))
            item = cookie.get("votesphere_results")

            if item and item.value in valid_result_tokens:
                return item.value

        except Exception:
            pass

        return None

    def make_options(self):
        options = []

        for i, candidate in enumerate(candidates):
            options.append(f"""
            <label class="block cursor-pointer">
                <input
                    type="radio"
                    name="candidate"
                    value="{i}"
                    required
                    class="candidate-radio peer sr-only"
                >

                <span class="candidate-card flex items-center gap-4
                    rounded-2xl border border-slate-700
                    bg-slate-950 p-4 transition
                    hover:border-violet-400 hover:bg-slate-800">

                    <span class="grid h-12 w-12 shrink-0
                        place-items-center rounded-xl
                        bg-violet-500/15 text-2xl">
                        {symbols[i]}
                    </span>

                    <span class="font-bold text-white">
                        {escape(candidate)}
                    </span>

                    <span class="ml-auto text-xs font-bold
                        text-violet-300">
                        SELECT →
                    </span>
                </span>
            </label>
            """)

        return "\n".join(options)

    def make_results(self):
        total = sum(votes)
        results = ""

        for i, candidate in enumerate(candidates):
            percentage = (
                round(votes[i] * 100 / total, 1)
                if total else 0
            )

            results += f"""
            <div>
                <div class="mb-2 flex flex-col gap-1
                    sm:flex-row sm:items-center
                    sm:justify-between text-sm">

                    <span class="font-semibold">
                        {symbols[i]} {escape(candidate)}
                    </span>

                    <span class="text-slate-300">
                        {votes[i]} votes · {percentage}%
                    </span>
                </div>

                <div class="h-3 overflow-hidden rounded-full
                    bg-slate-700">

                    <div class="h-full rounded-full
                        bg-gradient-to-r from-violet-500
                        to-fuchsia-500"
                        style="width:{percentage}%">
                    </div>
                </div>
            </div>
            """

        return f"""
        <p class="mt-3 text-sm text-emerald-300">
            PIN verified. Results unlocked.
        </p>

        <div class="mt-8 space-y-7">
            {results}
        </div>

        <div class="mt-9 flex items-center justify-between
            rounded-2xl border border-slate-700
            bg-slate-950 p-5">

            <div>
                <p class="text-sm text-slate-400">
                    Total Votes Recorded
                </p>

                <p class="mt-2 text-3xl font-black
                    text-violet-300">
                    {total}
                </p>
            </div>

            <div class="grid h-12 w-12 place-items-center
                rounded-xl bg-cyan-500/10 text-2xl">
                📊
            </div>
        </div>

        <form action="/lock-results" method="POST" class="mt-5">
            <button type="submit"
                class="rounded-xl border border-slate-700
                px-4 py-2 text-sm font-bold hover:bg-slate-800">
                Lock Results
            </button>
        </form>
        """

    def make_pin_form(self, error=""):
        error_html = ""
        # After a wrong PIN the panel must be visible, or the error is hidden
        panel_class = "" if error else "hidden"

        if error:
            error_html = f"""
            <p class="mt-3 rounded-xl border border-red-500/30
                bg-red-500/10 p-3 text-sm text-red-300">
                {escape(error)}
            </p>
            """

        return f"""
        <p class="mt-3 text-sm leading-6 text-slate-400">
            Click View Results and enter the PIN to see vote counts.
        </p>

        <div id="results-pin-panel"
            class="mt-6 rounded-2xl border border-slate-700
            bg-slate-950 p-5 {panel_class}">

            <form action="/results" method="POST">
                <label for="results-pin"
                    class="mb-2 block text-sm font-semibold">
                    Enter Results PIN
                </label>

                <input id="results-pin"
                    name="pin"
                    type="password"
                    autocomplete="off"
                    maxlength="64"
                    required
                    placeholder="Enter PIN"
                    class="w-full rounded-xl border border-slate-700
                    bg-slate-900 px-4 py-4 text-white
                    outline-none placeholder:text-slate-500
                    focus:border-violet-500">

                {error_html}

                <button type="submit"
                    class="mt-4 w-full rounded-xl bg-violet-600
                    px-5 py-4 font-bold hover:bg-violet-500">
                    Unlock Results →
                </button>
            </form>
        </div>

        <p class="mt-4 text-xs text-slate-500">
            Vote counts stay hidden unless the server verifies the PIN.
        </p>
        """

    def render_page(self, message="", pin_error=""):
        try:
            page = (BASE_DIR / "index.html").read_text(encoding="utf-8")

        except FileNotFoundError:
            self.send_html(
                "<h1>index.html not found. Keep both files in the same folder.</h1>",
                500
            )
            return

        if self.get_result_token():
            results_html = self.make_results()
        else:
            results_html = self.make_pin_form(pin_error)

        page = page.replace("{{TOTAL}}", str(sum(votes)))
        page = page.replace("{{COUNT}}", f"{len(candidates):02d}")
        page = page.replace("{{OPTIONS}}", self.make_options())
        page = page.replace("{{RESULTS}}", results_html)
        # The message is inserted LAST so that text inside it can never
        # be treated as a template placeholder.
        page = page.replace("{{MESSAGE}}", escape(message or DEFAULT_MESSAGE))

        self.send_html(page)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path not in ("/", "/index.html"):
            self.send_error(404, "Page not found")
            return

        code = parse_qs(parsed.query).get("m", [""])[0]
        self.render_page(message=MESSAGES.get(code, ""))

    def do_POST(self):
        path = urlparse(self.path).path
        form = self.read_form()

        # VOTE SUBMISSION
        if path == "/vote":
            voter_id = form.get("voter_id", [""])[0].strip()
            choice = form.get("candidate", [""])[0]

            if (
                not voter_id.isascii()
                or not voter_id.isdigit()
                or not (1 <= len(voter_id) <= 12)
            ):
                code = "bad_id"

            else:
                # "7", "07" and "007" are the same voter
                voter_key = str(int(voter_id))

                if voter_key in voted_users:
                    code = "dup"

                else:
                    try:
                        index = int(choice)

                        if not 0 <= index < len(candidates):
                            raise ValueError

                        votes[index] += 1
                        voted_users.add(voter_key)
                        code = "ok"

                    except (ValueError, TypeError):
                        code = "bad_choice"

            self.redirect("/?m=" + code)
            return

        # RESULTS PIN VERIFICATION
        if path == "/results":
            ip = self.client_address[0]
            fails, locked_until = pin_attempts.get(ip, [0, 0])
            now = time.time()

            if locked_until > now:
                wait = int(locked_until - now) + 1
                self.render_page(
                    message="Too many wrong PINs.",
                    pin_error=f"Locked. Try again in {wait} seconds."
                )
                return

            supplied_pin = form.get("pin", [""])[0]

            if pin_matches(supplied_pin, RESULTS_PIN):
                pin_attempts.pop(ip, None)
                token = secrets.token_urlsafe(32)
                valid_result_tokens.add(token)

                self.redirect(
                    "/",
                    headers=[
                        (
                            "Set-Cookie",
                            f"votesphere_results={token}; "
                            "HttpOnly; SameSite=Strict; Path=/"
                        )
                    ]
                )

            else:
                fails += 1

                if fails >= MAX_PIN_ATTEMPTS:
                    pin_attempts[ip] = [0, now + LOCK_SECONDS]
                else:
                    pin_attempts[ip] = [fails, 0]

                self.render_page(
                    message="Incorrect PIN. Please try again.",
                    pin_error="Incorrect PIN. Votes are still hidden."
                )

            return

        # LOCK RESULTS
        if path == "/lock-results":
            token = self.get_result_token()

            if token:
                valid_result_tokens.discard(token)

            self.redirect(
                "/",
                headers=[
                    (
                        "Set-Cookie",
                        "votesphere_results=; HttpOnly; "
                        "SameSite=Strict; Path=/; Max-Age=0"
                    )
                ]
            )
            return

        self.send_error(404, "Page not found")


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), VotingServer)

    print(f"VoteSphere is running at http://{HOST}:{PORT}")

    if RESULTS_PIN == DEFAULT_PIN:
        print("WARNING: using the default Results PIN 1234. "
              "Set the RESULTS_PIN environment variable to change it.")

    print("Press Ctrl+C to stop the server.")

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nServer stopped.")

    finally:
        server.server_close()
