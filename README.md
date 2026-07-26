# Community Activity Tracker Bot

A Telegram bot that tracks how active your community members are — messages,
task participation (like/comment/repost campaigns with screenshot proof), and
manual admin awards — and keeps a running leaderboard. Built for reward
cycles (e.g. rewarding the top members every 3 months).

## What it does

- **Passive tracking**: every text message earns a small point (capped per
  day, so spamming doesn't inflate scores).
- **Tasks**: an admin posts a task (e.g. "like, comment and repost this
  post") with `/newtask`. Members submit proof by sending a screenshot in
  the group. Admins review with `/pending`, then `/approve` or `/reject`.
- **Manual points**: admins can `/addpoints` or `/removepoints` for
  anything else (games, contests, following instructions well, etc.)
- **Leaderboard**: `/leaderboard` shows the top 10 live. `/myscore` shows
  an individual's points and rank.
- **Season reports**: `/report` dumps the full ranked list (as a file if
  long). `/resetseason` generates a final report, then zeroes points so you
  can start a fresh 3-month cycle.

## 1. Create the bot

1. Open Telegram, message **@BotFather**.
2. Send `/newbot` and follow the prompts to name it.
3. BotFather gives you a **token** like `123456:ABC-DEF...` — save it.
4. Add the bot to your group, and give it **admin rights** in the group
   (so it can read all messages, not just commands — in Telegram group
   privacy settings, or disable "Group Privacy" in BotFather's bot
   settings so it can see every message).

## 2. Find your Telegram user ID (to be a bot admin)

Message **@userinfobot** on Telegram — it replies with your numeric ID.
Anyone in this list can use admin commands regardless of their Telegram
group admin status. (Actual Telegram group admins/owners are automatically
treated as bot admins too — no extra setup needed for them.)

## 3. Run it

### Locally / on your own server

```bash
pip install -r requirements.txt

export BOT_TOKEN="123456:ABC-DEF..."       # from BotFather
export ADMIN_IDS="111111111,222222222"     # your Telegram user IDs, comma-separated

python bot.py
```

The bot uses polling, so it just needs to keep running — no public URL or
webhook needed.

### Free cloud hosting (so it runs 24/7 without your computer on)

Any host that can run a small always-on Python process works, e.g.
**Railway**, **Render** (background worker, not web service), or a
**PythonAnywhere** always-on task. General steps for all of them:

1. Push this folder to a GitHub repo.
2. Create a new project/service from that repo.
3. Set the start command to `python bot.py`.
4. Add the same `BOT_TOKEN` and `ADMIN_IDS` environment variables in the
   host's dashboard.
5. Deploy.

Data is stored in a local SQLite file (`tracker.db`), created automatically
next to `bot.py`. Most of these hosts wipe local disk on redeploy, so check
your host's docs for a persistent volume/disk if you want data to survive
redeploys — otherwise back up `tracker.db` periodically.

## 4. Day-to-day usage

**Launching an activity:**
```
/newtask 10 Like, comment and repost our latest Instagram post: <link>
```
Members reply in the group with a screenshot. You'll see it queue up:
```
/pending
/approve 4
```

**Rewarding something that isn't a formal task** (e.g. a game winner):
```
/addpoints 8
```
(sent as a reply to that member's message — awards 8 pts with reason "admin adjustment"),
or
```
/addpoints 123456789 8 won the trivia game
```

**Checking standings:**
```
/leaderboard      → top 10 right now
/myscore          → a member checks their own rank
/report           → full ranked list, for you
```

**Ending a 3-month cycle:**
```
/resetseason
```
This posts the final report, then resets everyone's points to 0 so the next
cycle starts clean (message counts are preserved for reference).

## Customizing

Open `bot.py` and adjust the constants near the top:

- `POINTS_PER_MESSAGE` — points per text message (default: 1)
- `MAX_MESSAGE_POINTS_PER_DAY` — daily cap from plain messages (default: 20)

Task point values are set per-task when you run `/newtask <points> <description>`.

## Notes & limitations

- The bot can't verify likes/comments/reposts on Instagram, TikTok, etc.
  directly (those platforms don't allow that from a Telegram bot) — that's
  why proof is via screenshot + admin approval, which keeps a human in the
  loop against fake submissions.
- Games are set up here as a "manually award points" workflow
  (`/addpoints`). If you want a specific game mechanic wired up
  automatically (trivia, quizzes, etc.), that's a natural next step —
  just say what the game should be and I can add it.
