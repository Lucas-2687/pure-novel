"""Vercel Python function adapter; local server remains available in reader.py."""
from reader import Handler

class handler(Handler):
    """Explicit entrypoint discovered by the Vercel Python builder."""