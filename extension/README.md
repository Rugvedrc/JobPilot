# FresherPilot Autofill (browser extension)

Fills job application forms and auto-applies, **in your own browser** with your existing logins.

## Install (Chrome, Edge or Brave)
1. Start the app: `python main.py` (http://localhost:8000).
2. Open `edge://extensions` or `chrome://extensions`, turn on **Developer mode**.
3. **Load unpacked** -> pick this `extension` folder. (Already installed? Click the reload icon.)
4. Refresh the FresherPilot page.

## Auto-apply
- Each job in the app has an **Auto-apply** button; **Auto-apply all** (top bar) does your best matches one by one.
- It opens the job in a tab of your browser, labels the buttons (yellow keys), lets the AI pick what to press,
  fills forms, and submits. Pauses of a few seconds between steps and 45-90 s between jobs.
- If a site needs you (sign in, CAPTCHA, a code, a question your profile can't answer) the tab comes to the front
  and the app shows **Continue / Skip this job / Stop**. Do it in the tab, then click Continue.

## Fill only
Open an application form, click the extension -> **Fill this page** (or Alt+Shift+F). You review and submit.
