"""
Test BCGame session + dump localStorage to find auth token.
"""
import json
import sys
from playwright.sync_api import sync_playwright

SESSION_FILE = "/root/crash-collector/bcgame_session.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        storage_state=SESSION_FILE,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    page = ctx.new_page()
    page.goto("https://bcgame61.com/game/crash", timeout=30000)
    page.wait_for_timeout(6000)

    print("URL:", page.url)

    # Dump all localStorage keys
    ls = page.evaluate("() => { let r={}; for(let i=0;i<localStorage.length;i++){let k=localStorage.key(i); r[k]=localStorage.getItem(k);} return r; }")
    print(f"\nlocalStorage keys ({len(ls)}):")
    for k, v in ls.items():
        short_v = v[:80] if v else ""
        print(f"  {k}: {short_v}")

    # Check login status
    logged_in = page.evaluate("""() => {
        const keys = ['token','userToken','user_token','access_token','authToken','userData','user'];
        for(const k of keys){
            const v = localStorage.getItem(k);
            if(v) return {key:k, value:v.substring(0,60)};
        }
        return null;
    }""")
    print(f"\nAuth token in localStorage: {logged_in}")

    # Check page for balance/username
    try:
        text = page.locator("body").text_content(timeout=3000)
        if "$" in text and "Sign In" not in text[:500]:
            print("\nStatus: LOGGED IN (balance visible)")
        else:
            print("\nStatus: NOT logged in")
    except Exception:
        pass

    page.screenshot(path="/root/crash-collector/session_test.png")
    browser.close()
