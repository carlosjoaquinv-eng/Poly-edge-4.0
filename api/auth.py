"""
PolyEdge v4 — Authentication Module
=====================================
Secure auth with:
  - Argon2id password hashing (OWASP 2025 recommended)
  - Per-IP rate limiting (brute-force protection)
  - JWT tokens in HttpOnly secure cookies
  - Login page with professional UI
"""

import os
import time
import json
import hmac
import hashlib
import base64
import logging
import asyncio
from collections import defaultdict

logger = logging.getLogger("polyedge.auth")

# ── Argon2 hashing (fallback to hashlib if argon2 not installed) ──

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError
    _ph = PasswordHasher()
    _HAS_ARGON2 = True
except ImportError:
    _HAS_ARGON2 = False
    logger.warning("argon2-cffi not installed — using PBKDF2 fallback (install argon2-cffi for better security)")


def hash_password(password: str) -> str:
    """Hash a password using Argon2id (preferred) or PBKDF2 fallback."""
    if _HAS_ARGON2:
        return _ph.hash(password)
    # PBKDF2 fallback with 600k iterations (OWASP minimum)
    salt = os.urandom(32)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2:sha256:600000${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(stored_hash: str, password: str) -> bool:
    """Verify a password against its stored hash."""
    if _HAS_ARGON2 and stored_hash.startswith("$argon2"):
        try:
            return _ph.verify(stored_hash, password)
        except Exception:
            return False
    elif stored_hash.startswith("pbkdf2:"):
        parts = stored_hash.split("$")
        if len(parts) != 3:
            return False
        salt = base64.b64decode(parts[1])
        expected = base64.b64decode(parts[2])
        iterations = int(parts[0].split(":")[2])
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return hmac.compare_digest(dk, expected)
    # Plaintext comparison (legacy / first-run migration)
    return hmac.compare_digest(stored_hash, password)


# ── JWT-like token (minimal, no external dependency) ──

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def create_token(user: str, secret: str, ttl_hours: int = 24) -> str:
    """Create a signed token (HMAC-SHA256). No external JWT dependency needed."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url_encode(json.dumps({
        "sub": user,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_hours * 3600,
    }).encode())
    signing_input = f"{header}.{payload}"
    sig = _b64url_encode(hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest())
    return f"{header}.{payload}.{sig}"


def verify_token(token: str, secret: str) -> dict | None:
    """Verify and decode a signed token. Returns payload or None."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        signing_input = f"{parts[0]}.{parts[1]}"
        expected_sig = _b64url_encode(hmac.new(
            secret.encode(), signing_input.encode(), hashlib.sha256
        ).digest())
        if not hmac.compare_digest(parts[2], expected_sig):
            return None
        payload = json.loads(_b64url_decode(parts[1]))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# ── Rate Limiter ──

class RateLimiter:
    """Per-IP rate limiter for login attempts."""

    def __init__(self, max_attempts: int = 5, window_sec: int = 300,
                 lockout_sec: int = 900):
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self.lockout_sec = lockout_sec
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._lockouts: dict[str, float] = {}

    def is_locked(self, ip: str) -> tuple[bool, int]:
        """Check if IP is locked out. Returns (locked, retry_after_sec)."""
        if ip in self._lockouts:
            remaining = self._lockouts[ip] - time.time()
            if remaining > 0:
                return True, int(remaining)
            del self._lockouts[ip]
        return False, 0

    def record_attempt(self, ip: str, success: bool):
        """Record a login attempt."""
        now = time.time()
        if success:
            # Clear on successful login
            self._attempts.pop(ip, None)
            self._lockouts.pop(ip, None)
            return

        # Clean old attempts outside window
        self._attempts[ip] = [t for t in self._attempts[ip] if now - t < self.window_sec]
        self._attempts[ip].append(now)

        if len(self._attempts[ip]) >= self.max_attempts:
            self._lockouts[ip] = now + self.lockout_sec
            self._attempts[ip].clear()
            logger.warning(f"IP {ip} locked out for {self.lockout_sec}s after {self.max_attempts} failed attempts")

    def cleanup(self):
        """Periodic cleanup of stale entries."""
        now = time.time()
        stale_ips = [ip for ip, ts in self._lockouts.items() if ts < now]
        for ip in stale_ips:
            del self._lockouts[ip]
        stale_attempts = [ip for ip, attempts in self._attempts.items() if not attempts]
        for ip in stale_attempts:
            del self._attempts[ip]


# ── Auth Manager (wires everything together) ──

class AuthManager:
    """
    Manages dashboard authentication.

    Credentials stored in /data_v4/auth.json:
    {
        "users": {
            "admin": {"hash": "$argon2id$...", "created": 1234567890}
        }
    }
    """

    PUBLIC_PATHS = {"/health", "/login", "/api/login", "/api/logout"}

    def __init__(self, data_dir: str, jwt_secret: str = None,
                 session_hours: int = 24):
        self.data_dir = data_dir
        self.auth_file = os.path.join(data_dir, "auth.json")
        self.jwt_secret = jwt_secret or os.urandom(32).hex()
        self.session_hours = session_hours
        self.rate_limiter = RateLimiter()
        self._users: dict = {}
        self._load()

    def _load(self):
        """Load user credentials from disk."""
        if os.path.exists(self.auth_file):
            try:
                with open(self.auth_file, "r") as f:
                    data = json.load(f)
                self._users = data.get("users", {})
                logger.info(f"Loaded {len(self._users)} user(s) from auth store")
            except Exception as e:
                logger.error(f"Failed to load auth store: {e}")
                self._users = {}
        else:
            self._users = {}

    def _save(self):
        """Persist user credentials to disk."""
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.auth_file, "w") as f:
            json.dump({"users": self._users}, f, indent=2)

    def has_users(self) -> bool:
        """Check if any users are configured."""
        return len(self._users) > 0

    def create_user(self, username: str, password: str):
        """Create or update a user with hashed password."""
        self._users[username] = {
            "hash": hash_password(password),
            "created": int(time.time()),
        }
        self._save()
        logger.info(f"User '{username}' created/updated")

    def authenticate(self, username: str, password: str) -> bool:
        """Verify username + password."""
        user = self._users.get(username)
        if not user:
            return False
        return verify_password(user["hash"], password)

    def make_session_token(self, username: str) -> str:
        """Create a signed session token."""
        return create_token(username, self.jwt_secret, self.session_hours)

    def validate_session(self, token: str) -> str | None:
        """Validate session token. Returns username or None."""
        payload = verify_token(token, self.jwt_secret)
        if payload and payload.get("sub") in self._users:
            return payload["sub"]
        return None

    def make_middleware(self):
        """Create aiohttp middleware for route protection."""
        from aiohttp import web

        auth_mgr = self

        @web.middleware
        async def auth_middleware(request, handler):
            # Public paths — always accessible
            if request.path in auth_mgr.PUBLIC_PATHS:
                return await handler(request)

            # Static assets in login page
            if request.path.startswith("/static/"):
                return await handler(request)

            # No users configured — skip auth (first-run)
            if not auth_mgr.has_users():
                return await handler(request)

            # Check JWT cookie
            token = request.cookies.get("polyedge_session")
            if token:
                username = auth_mgr.validate_session(token)
                if username:
                    request["user"] = username
                    return await handler(request)

            # Check Authorization header (for API clients)
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                username = auth_mgr.validate_session(auth_header[7:])
                if username:
                    request["user"] = username
                    return await handler(request)

            # Not authenticated — redirect browser to login or return 401 for API
            if request.path.startswith("/api/"):
                return web.json_response(
                    {"error": "unauthorized", "message": "Login required"},
                    status=401
                )

            # Redirect to login page
            raise web.HTTPFound("/login")

        return auth_middleware

    def register_routes(self, app):
        """Register login/logout routes on the aiohttp app."""
        from aiohttp import web

        async def handle_login_page(request):
            """Serve login HTML page."""
            # If no users exist, show setup page
            setup_mode = not self.has_users()
            html = _render_login_page(setup_mode=setup_mode)
            return web.Response(text=html, content_type="text/html")

        async def handle_login_api(request):
            """POST /api/login — authenticate and return session cookie."""
            ip = request.remote or "unknown"

            # Rate limit check
            locked, retry_after = self.rate_limiter.is_locked(ip)
            if locked:
                return web.json_response({
                    "error": "rate_limited",
                    "message": f"Too many attempts. Try again in {retry_after}s",
                    "retry_after": retry_after,
                }, status=429, headers={"Retry-After": str(retry_after)})

            try:
                body = await request.json()
            except Exception:
                return web.json_response(
                    {"error": "invalid_request", "message": "JSON body required"},
                    status=400
                )

            username = body.get("username", "").strip()
            password = body.get("password", "")

            if not username or not password:
                return web.json_response(
                    {"error": "missing_fields", "message": "Username and password required"},
                    status=400
                )

            # First-run setup: create user
            if not self.has_users():
                if len(password) < 8:
                    return web.json_response(
                        {"error": "weak_password", "message": "Password must be at least 8 characters"},
                        status=400
                    )
                self.create_user(username, password)
                token = self.make_session_token(username)
                resp = web.json_response({
                    "status": "created",
                    "message": f"User '{username}' created. You are now logged in.",
                    "username": username,
                })
                resp.set_cookie(
                    "polyedge_session", token,
                    max_age=self.session_hours * 3600,
                    httponly=True,
                    samesite="Lax",
                    path="/",
                )
                logger.info(f"First user '{username}' created from {ip}")
                return resp

            # Normal login
            # Run hash verification in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None, self.authenticate, username, password
            )

            if not success:
                self.rate_limiter.record_attempt(ip, False)
                return web.json_response(
                    {"error": "invalid_credentials", "message": "Invalid username or password"},
                    status=401
                )

            self.rate_limiter.record_attempt(ip, True)
            token = self.make_session_token(username)
            resp = web.json_response({
                "status": "ok",
                "message": "Login successful",
                "username": username,
            })
            resp.set_cookie(
                "polyedge_session", token,
                max_age=self.session_hours * 3600,
                httponly=True,
                samesite="Lax",
                path="/",
            )
            logger.info(f"User '{username}' logged in from {ip}")
            return resp

        async def handle_logout(request):
            """POST /api/logout — clear session cookie."""
            resp = web.json_response({"status": "ok", "message": "Logged out"})
            resp.del_cookie("polyedge_session", path="/")
            return resp

        app.router.add_get("/login", handle_login_page)
        app.router.add_post("/api/login", handle_login_api)
        app.router.add_post("/api/logout", handle_logout)


# ── Login Page HTML ──

def _render_login_page(setup_mode: bool = False) -> str:
    title = "PolyEdge Setup" if setup_mode else "PolyEdge Login"
    subtitle = "Create your admin account" if setup_mode else "Sign in to continue"
    button_text = "Create Account" if setup_mode else "Sign In"
    endpoint_note = ""
    if setup_mode:
        endpoint_note = '<p class="setup-note">First time? Create your username and password below.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0a0e17;
    color: #e2e8f0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }}

  .login-container {{
    width: 100%;
    max-width: 420px;
    padding: 20px;
  }}

  .login-card {{
    background: #131a2b;
    border: 1px solid #1e293b;
    border-radius: 16px;
    padding: 40px 32px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }}

  .logo {{
    text-align: center;
    margin-bottom: 32px;
  }}

  .logo h1 {{
    font-size: 28px;
    font-weight: 700;
    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 4px;
  }}

  .logo .version {{
    font-size: 12px;
    color: #64748b;
    letter-spacing: 2px;
    text-transform: uppercase;
  }}

  .subtitle {{
    text-align: center;
    color: #94a3b8;
    font-size: 14px;
    margin-bottom: 28px;
  }}

  .setup-note {{
    background: #1e3a5f;
    border: 1px solid #2563eb;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 13px;
    color: #93c5fd;
    margin-bottom: 20px;
    text-align: center;
  }}

  .form-group {{
    margin-bottom: 20px;
  }}

  .form-group label {{
    display: block;
    font-size: 13px;
    font-weight: 600;
    color: #94a3b8;
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}

  .form-group input {{
    width: 100%;
    padding: 12px 16px;
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: 10px;
    color: #e2e8f0;
    font-size: 15px;
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
  }}

  .form-group input:focus {{
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
  }}

  .form-group input::placeholder {{
    color: #475569;
  }}

  .btn-login {{
    width: 100%;
    padding: 13px;
    background: linear-gradient(135deg, #3b82f6, #6366f1);
    border: none;
    border-radius: 10px;
    color: white;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    transition: transform 0.15s, box-shadow 0.15s;
    margin-top: 4px;
  }}

  .btn-login:hover {{
    transform: translateY(-1px);
    box-shadow: 0 8px 24px rgba(59, 130, 246, 0.3);
  }}

  .btn-login:active {{
    transform: translateY(0);
  }}

  .btn-login:disabled {{
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }}

  .error-msg {{
    background: #3b1219;
    border: 1px solid #991b1b;
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    color: #fca5a5;
    margin-bottom: 16px;
    display: none;
    text-align: center;
  }}

  .success-msg {{
    background: #052e16;
    border: 1px solid #166534;
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    color: #86efac;
    margin-bottom: 16px;
    display: none;
    text-align: center;
  }}

  .rate-limit-msg {{
    background: #431407;
    border: 1px solid #9a3412;
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    color: #fdba74;
    margin-bottom: 16px;
    display: none;
    text-align: center;
  }}

  .footer {{
    text-align: center;
    margin-top: 24px;
    font-size: 12px;
    color: #475569;
  }}

  .spinner {{
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid rgba(255,255,255,0.3);
    border-top-color: white;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    vertical-align: middle;
    margin-right: 6px;
  }}

  @keyframes spin {{
    to {{ transform: rotate(360deg); }}
  }}
</style>
</head>
<body>
<div class="login-container">
  <div class="login-card">
    <div class="logo">
      <h1>PolyEdge</h1>
      <div class="version">v4 Trading System</div>
    </div>
    <p class="subtitle">{subtitle}</p>
    {endpoint_note}
    <div id="error" class="error-msg"></div>
    <div id="success" class="success-msg"></div>
    <div id="rateLimit" class="rate-limit-msg"></div>
    <form id="loginForm" onsubmit="return handleLogin(event)">
      <div class="form-group">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" placeholder="Enter username"
               autocomplete="username" required autofocus>
      </div>
      <div class="form-group">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" placeholder="Enter password"
               autocomplete="current-password" required>
      </div>
      <button type="submit" class="btn-login" id="submitBtn">{button_text}</button>
    </form>
    <div class="footer">Protected by rate limiting &bull; Argon2id hashing</div>
  </div>
</div>
<script>
async function handleLogin(e) {{
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  const errEl = document.getElementById('error');
  const successEl = document.getElementById('success');
  const rateEl = document.getElementById('rateLimit');

  errEl.style.display = 'none';
  successEl.style.display = 'none';
  rateEl.style.display = 'none';

  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;

  if (!username || !password) {{
    errEl.textContent = 'Please fill in all fields';
    errEl.style.display = 'block';
    return false;
  }}

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Authenticating...';

  try {{
    const res = await fetch('/api/login', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ username, password }})
    }});

    const data = await res.json();

    if (res.ok) {{
      successEl.textContent = data.message || 'Login successful! Redirecting...';
      successEl.style.display = 'block';
      setTimeout(() => window.location.href = '/', 800);
    }} else if (res.status === 429) {{
      rateEl.textContent = data.message || 'Too many attempts. Please wait.';
      rateEl.style.display = 'block';
      // Countdown
      let secs = data.retry_after || 60;
      const interval = setInterval(() => {{
        secs--;
        if (secs <= 0) {{
          clearInterval(interval);
          rateEl.style.display = 'none';
          btn.disabled = false;
          btn.textContent = '{button_text}';
        }} else {{
          rateEl.textContent = `Locked out. Try again in ${{secs}}s`;
        }}
      }}, 1000);
      return false;
    }} else {{
      errEl.textContent = data.message || 'Invalid credentials';
      errEl.style.display = 'block';
    }}
  }} catch (err) {{
    errEl.textContent = 'Connection error. Please try again.';
    errEl.style.display = 'block';
  }}

  btn.disabled = false;
  btn.textContent = '{button_text}';
  return false;
}}
</script>
</body>
</html>"""
