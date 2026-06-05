"""VPS wrapper: headless=True with anti-detection + VPN routing."""
import sys, os
# Patch the launch to use headless=True with stealth flags
_ORIGINAL = __import__('bot_realbet')
