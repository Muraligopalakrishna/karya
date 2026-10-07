# Karya

**Your own AI agent that does the work.** Chat with it and it researches the web, finds and applies for jobs, tailors your resume for every application, finds freelance projects, emails people, posts on your social accounts, fixes your PC and builds websites. It runs on your Windows PC, with your own free AI keys and your own logins.

*Karya* (कार्य) means "work" or "task" in Hindi.

## What it can do

- **Jobs, straight from the companies.** `find_jobs` searches company career sites directly, in any country: Workday (Salesforce, Adobe, Nvidia, Cisco, Accenture, PwC, Walmart, Citi, Barclays and about 50 more), the Greenhouse / Lever / Ashby / SmartRecruiters boards of about 170 product and service companies, Amazon, Microsoft, Google, Netflix and Atlassian, YC startups, Hacker News "Who is hiring", recently funded startups, Indian boards (Instahyre, Cutshort, foundit), The Muse, Arbeitnow and remote boards. A web search finds more company job pages for your role and place, and Karya remembers those companies for next time. LinkedIn is only a fallback when few jobs turn up (or when you ask). Filter by product, service, startup or enterprise companies. Every job gets a match score with reasons, and company sites rank above job boards.
- **Recently funded companies.** `find_funded_companies` reads funding news from any country (Google News, TechCrunch, Crunchbase News, tech.eu, Inc42, YourStory) and Y Combinator's latest batches, ranks the companies for you (a fresh round, Series A-C, your country and field, open roles that fit), finds each one's careers page and lists their matching jobs.
- **Your companies.** Add any company by name or careers-page URL in your job preferences; Karya finds its careers system once and checks it on every search.
- **It remembers what you applied to.** Every application goes into a tracker on your PC. New searches leave out jobs you already applied to, and companies you applied to in the last 60 days, so each session brings new companies. Name a company (or ask for them) to see it again. Karya never submits the same job twice.
- **Applying.** It reads the posting and the form's questions, writes a cover letter, builds a resume tailored to that job (PDF), fills the form and submits after you approve. Like autofill apps, it fills everything it already knows in one step (name, contact details, links, location, the tailored resume, your earlier answers), so the AI only handles the questions that are left. It never adds a skill you haven't approved, and it logs every application.
- **No resume yet?** It builds one with you by asking short questions.
- **Accounts.** Save logins in Settings, or Karya asks when a site needs one. Passwords are encrypted on your PC with Windows DPAPI and never sent to the AI. Karya types them into the page itself, and only on the matching website. It can also create new accounts for you with strong generated passwords.
- **Research and outreach.** Web and news search, reading pages, and `find_contacts` (public emails and social links for any website), then personal emails that you approve one by one.
- **Markets.** Stock prices, history, news and market overview (NSE/BSE, US and more).
- **What people are saying about a market.** `market_sentiment` reads what traders post about any stock, coin, forex pair, index or topic, all at once:
  - StockTwits, where traders label their own posts bullish or bearish
  - the market's subreddits on Reddit
  - X, searched in Karya's browser with your login
  - TradingView ideas (long or short)
  - investor forums: ValuePickr for India, Hacker News for tech
  - YouTube video titles and the news; LinkedIn posts too, when you ask for them

  You get the crowd's mood with counts per source, the price levels traders mention (targets, stops, support, resistance), what they're talking about, and real quotes with links. It reports opinions, not advice.
- **Crawl any website.** `crawl_site` goes through a whole site and returns the pages and passages about your topic: forums, blogs, news, docs and company sites. Name a site ("linkedin", "x", "reddit") with your words and it starts at that site's own search. Public sites are read over plain HTTP, following robots.txt. Sites that need a login (LinkedIn, X, Facebook, Instagram) are read in Karya's browser, where you're logged in, a few seconds per page and at most 30 pages per crawl. It only reads: it never clicks, posts, follows or logs in, and it stops at login pages and security checks.
- **Browser automation** like rtrvr.ai: posting on LinkedIn, X, Reddit and more, filling forms, any site you're logged into. Pages without buttons work too: canvas, maps and game boards (it can play chess on chess.com and lichess).
- **PC help.** Diagnose and fix problems, manage files, run commands (it asks first).
- **Websites.** Build, preview and publish to Vercel.

## Install (Windows 10/11)

1. Install [Python 3.11+](https://www.python.org/downloads/) (tick "Add to PATH") and Google Chrome.
2. Download Karya: on https://github.com/Muraligopalakrishna/karya click **Code → Download ZIP** and unzip it (or `git clone https://github.com/Muraligopalakrishna/karya.git`), then double-click **`start.bat`**. The first run installs everything.
3. The chat opens in your browser. Click **Setup** and add any AI key you have, free or paid: OpenAI, Anthropic Claude, Google Gemini, Groq, OpenRouter, DeepSeek, Mistral, xAI, Together, Cerebras, or any OpenAI-compatible API. Karya checks each key and learns its limits. On a small free plan it sends small requests and pauses at the limit; on a big paid plan it runs at full speed. Free options:
   - Gemini: https://aistudio.google.com/apikey
   - Groq: https://console.groq.com/keys
   - OpenRouter (free models): https://openrouter.ai/keys

   **Kiro subscription:** paste a Kiro API key (`ksk_...`, from app.kiro.dev → API keys; Pro plans and up) into the Kiro field. Kiro keys aren't OpenAI-style keys, so Karya runs them through the official Kiro CLI (install it from kiro.dev), with its own private settings folder. The default model is Qwen3 Coder Next (0.05× credits), with GLM-5 as the backup; set `KIRO_MODEL` to change it.
4. Optional, in the same Setup panel:
   - your Gmail address plus an [App Password](https://myaccount.google.com/apppasswords), to send and read email
   - your resume path
   - a Vercel token, to publish websites

**Everyone uses their own keys and accounts.** Nothing is shared and there is no Karya server: your settings live in `.env` and your data lives in `data/`, both on your PC and both excluded from git.

There's also an offline mode: install [Ollama](https://ollama.com), then run `ollama pull qwen3.5:4b` and `ollama create karya-qwen -f Modelfile`. It's free and private, but much slower on PCs without a GPU.

## Use your own Chrome (Karya Browser Link)

By default Karya browses in its own Chrome window. To let it work in **your** Chrome, with the logins you already have, add the small extension in `karya/extension` once:

1. Open `chrome://extensions` in Chrome and turn on **Developer mode** (top right).
2. Click **Load unpacked** and choose the `karya\extension` folder inside your Karya folder. Setup shows the exact path and has a copy button.

That's it. Whenever Karya is running, the extension connects by itself and Setup shows "Your Chrome: connected".

- Karya opens its own tab in a purple **Karya** tab group and works only there. It can't see or touch your other tabs, unless you click "Let Karya use this tab" in the extension's menu.
- Approvals still happen in Karya's chat. The extension icon shows a red **!** when Karya is waiting for you.
- On game boards, maps and canvas, sites like chess.com ignore simulated clicks, so the extension uses Chrome's debugger to send real mouse clicks. While it does, Chrome shows a "Karya Browser Link started debugging this browser" bar. It disappears 20 seconds after the last such action.
- Under Setup → "Browser Karya uses" you can pick your Chrome only, Karya's own window only, or automatic (the default).

Chrome doesn't let programs control your normal profile directly, so this extension is the supported way to do it. It talks only to Karya on `127.0.0.1`, using your install's private token.

## Use Karya from other AI apps and CLIs (MCP)

Claude Desktop, Cursor, Kiro, VS Code, Windsurf, OpenClaw, Claude Code, Codex, Gemini CLI and other MCP apps can use Karya's tools with **their own AI model**: browsing and posting in a real Chrome, logins from your vault, email, job search, tailored resumes and applications. Setup → "Use Karya from other AI apps and CLIs" shows the exact config with copy buttons. For apps with a config file it looks like this (use your Karya folder):

```json
{ "mcpServers": { "karya": { "command": "C:\\Karya\\.venv\\Scripts\\python.exe", "args": ["C:\\Karya\\karya_mcp.py"] } } }
```

For CLIs it's one command, for example `claude mcp add --scope user karya -- "C:\Karya\.venv\Scripts\python.exe" "C:\Karya\karya_mcp.py"`. Codex, Gemini CLI, Qwen Code and Kiro CLI have their own versions in Setup.

- The AI app starts Karya by itself if it isn't running.
- Karya needs no AI key of its own here: the app's model does the thinking, including writing tailored resumes. Karya checks every fact against your real resume (no invented jobs, skills or numbers) and makes the PDF.
- **The Chrome extension is optional.** Without it Karya works in its own Chrome window (log in to your sites there once). With it Karya uses your everyday Chrome and its logins.
- Every check still runs, whichever AI is in charge:
  - approvals for sending, posting, submitting, paying and deleting (inside the app when it supports MCP approvals, otherwise in Karya's window, which opens by itself)
  - empty required fields, CAPTCHAs, guessed answers
  - made-up, bounced or duplicate emails, and one email per business
  - the same post twice
  - daily limits: 40 emails, 10 posts and 30 applications by default, changeable in Setup
- Submits, posts and bids come back as `RESULT: SUBMITTED / NOT SUBMITTED / UNCONFIRMED`. `karya_activity` lists what really happened today.
- 40 tools are shared by default (Cursor allows about 40 in total). Pick others with `KARYA_MCP_TOOLS`, e.g. `browser,email`, or `all`.
- Passwords never reach the other AI. Setup has a switch to turn MCP access off.
- Some apps stop a tool call after a minute. Set a longer tool timeout (Codex: `tool_timeout_sec = 900`) so there's time to approve.

## Safety

- **Full access (autopilot).** Off by default. Turn it on in Setup and Karya does everything without stopping to ask - fill forms, send, post, apply, run commands. Its quality checks still run (empty required fields, duplicate or made-up emails, the same post twice, daily limits, and it still verifies that a submit/post actually went through). Real-money payments and deleting an account or data still ask first, unless you also switch that floor off. There's a per-run cap so a loop can't fire hundreds of actions.
- **One login for everything.** In Setup you can save a primary login (email + password) and turn on "reuse one login across sites". Karya then signs in and creates accounts on job portals and other sites with it, without asking each time. For new sign-ups it can reuse that password, or (safer) generate and store a unique one per site so you never type them.
- **Posting done right.** Before posting, Karya reads a short playbook (`how_to_post`) and follows it in order. With a video or photo, it attaches the file first, waits until it's processed, then types the text, and it won't click Post without the file you gave it. It picks the file itself, so the Windows file dialog never opens. On Instagram it keeps the video's original size (picks "Original", not a square crop) and keeps the audio on. Posting on X, LinkedIn, Reddit and Instagram is free; X Premium is not needed to post.
- Karya shows an **Approve / Deny** card before it sends an email, posts, submits an application, pays, deletes or publishes (unless full access is on). Commands and file changes ask too, unless you tick "Ask only for critical actions".
- Before a Submit click Karya checks for empty required fields. Afterwards it reads the page and reports SUBMITTED, NOT SUBMITTED (with the page's errors) or UNCONFIRMED. It only says "submitted" when the site confirms it.
- Application questions are answered from your resume and profile. Anything they don't cover (notice period, salary, years of a specific experience) Karya asks you; it never guesses.
- When you pick several jobs, Karya keeps the list itself and goes through all of them, then shows what was submitted, skipped (and why) or still open. The Submit card names the job and lists every answer the form will send.
- CAPTCHAs ("I'm not a robot") are left for you to solve; Karya never ticks them.
- Passwords never pass through the chat or the AI. They're typed into pages only on the site they belong to; anything else needs your explicit approval.
- Web pages and emails are treated as untrusted data, never as instructions.
- The chat only listens on `127.0.0.1` behind a private token link. Every action is logged in `data/logs/actions.jsonl`.
- Karya refuses to read its own secret files (`.env`, the vault, the token) and OS credential stores (SSH keys, the browser's password store), and it scrubs API keys, the token and the vault out of any tool output before the AI, the chat or the logs see them. Passwords are encrypted with Windows DPAPI and never go to the AI. Ask Karya to "run a security check" for a plain-language report.
- Use automation responsibly. LinkedIn, X and similar sites limit automated activity: don't mass-apply or mass-post, and respect each site's terms.

## Development

```
.venv\Scripts\python.exe -m pytest tests -q
```

- `KARYA_SKIP_LIVE=1` skips tests that use the internet.
- `KARYA_RUN_OLLAMA=1` adds the slow offline-model test.

| Path | What's there |
|---|---|
| `karya/agent.py` | the agent loop |
| `karya/llm.py` | multi-provider AI client |
| `karya/tools/` | all tools |
| `karya/vault.py` | encrypted accounts |
| `karya/static/` | chat UI |

To add a tool, write a function with the `@tool(...)` decorator and give it a risk level. The agent picks it up automatically.

## License

MIT, see [LICENSE](LICENSE).
