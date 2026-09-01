"""
Shared Flask extension instances.

CSRFProtect and Limiter are created here -- unbound to any app -- rather
than directly on `app` inside app.py, so that blueprints defined in other
files (auth.py, auth_email.py) can import and use them too (e.g. to
rate-limit the login route) without importing app.py and risking a
circular import. app.py calls `.init_app(app)` on both during startup.
"""
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

csrf = CSRFProtect()

# In-memory store -- fine for a single-process deployment (matches the
# rest of this app's "no Redis needed" approach). Default limit applies
# to every route; individual routes tighten this further where it matters
# (chat endpoints, auth endpoints).
limiter = Limiter(get_remote_address, default_limits=["200 per minute"], storage_uri="memory://")
