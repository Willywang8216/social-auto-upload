# agent-manager: Controlling Another Session (Guide)

How to create another agent session in agent-manager and drive it from your
own session — sending commands, reading feedback, and coordinating file access.

---

## 1. Spawn a session

```bash
# Minimal — starts a pi session in the current directory
agent-manager spawn --name "fix-tests" --prompt "Run pytest and fix failures"

# More control
agent-manager spawn \
  --name "frontend-task" \
  --tool pi \                          # cli: pi, claude, etc.
  --directory /home/will/social-auto-upload \
  --group my-fleet \                   # optional: files them together in the user's list
  --worktree \                         # optional: gives it its own git worktree
  --json                               # machine-readable output → gives you the session id
```

Key point: **use `--json` and capture the id** — every follow-up command needs it.

```bash
SID=$(agent-manager spawn --name "fix-tests" --prompt "..." --json | jq -r '.id')
```

## 2. Send commands to it

```bash
agent-manager send "$SID" "Now run the backend tests and report results"
```

- The message is **queued** and typed in once that agent is *at rest* — so it
  never lands on an approval prompt mid-flight.
- Returns a **message id**; you can track delivery:

```bash
MSG=$(agent-manager send "$SID" "..." --json | jq -r '.message_id')
agent-manager message-status "$MSG"     # queued / held / delivered / dropped / answered
```

## 3. Read feedback

Three options, in increasing order of efficiency:

```bash
# a) Peek at its screen right now
agent-manager read "$SID"

# b) Block until it stops working (better than polling read in a loop)
agent-manager wait "$SID" --until idle --timeout 5m

# c) Then read the final output
agent-manager read "$SID"
```

`wait` exits non-zero on timeout, so you can branch on it:

```bash
if agent-manager wait "$SID" --until idle --timeout 10m; then
  agent-manager read "$SID"
else
  echo "still busy, checking anyway..."
  agent-manager read "$SID"
fi
```

## 4. Typical control loop (as a script)

```bash
#!/usr/bin/env bash
set -euo pipefail

SID=$(agent-manager spawn --name "audit-uploader" \
        --prompt "Audit uploader/ for hardcoded selectors and list findings" \
        --json | jq -r '.id')

agent-manager wait "$SID" --until idle --timeout 10m

agent-manager send "$SID" "Now fix the first finding and run the tests"
agent-manager wait "$SID" --until idle --timeout 10m

agent-manager read "$SID"      # grab the report
```

## 5. Housekeeping

```bash
agent-manager sessions          # see everything running (also --json)
agent-manager kill "$SID"       # stop it
agent-manager revive "$SID"     # bring it back on its old row, same conversation
agent-manager archive "$SID"    # file it away when done (--restore to undo)
```

## Coordination bits worth knowing

- **`task` list** — before spawning, `agent-manager task create "title" --body "..."`;
  your child can `agent-manager task claim` it. Good for handing off discrete
  work items rather than chat messages.

  ```bash
  agent-manager task create "Publish video to all platforms" --body "..."
  # in child: agent-manager task claim
  ```

- **`reserve`** — if you *and* the other session will touch the same files,
  declare them first:

  ```bash
  agent-manager reserve uploader/douyin.py --mode exclusive --note "refactoring login"
  agent-manager reservations        # see what others hold
  agent-manager release-files       # when done
  ```

  This prevents two agents editing the same file blindly.

- **Groups** — `agent-manager create-group fleet-a` then `--group fleet-a`
  on spawn, so the user sees your fleet together in their list.

- **Managed terminals** — for work the user should watch (SSH, servers):

  ```bash
  agent-manager terminal create
  agent-manager terminal send <id> --command "tail -f logs/app.log"
  agent-manager terminal read <id>
  agent-manager terminal close <id>
  ```

---

## End-to-end example

```bash
#!/usr/bin/env bash
set -euo pipefail

# 1. spawn
SID=$(agent-manager spawn --name "publish-job" \
        --prompt "Publish today's video to all platforms" \
        --json | jq -r '.id')

# 2. wait for it to finish its first pass
agent-manager wait "$SID" --until idle --timeout 15m

# 3. send follow-up
agent-manager send "$SID" "Re-run only the failed targets and report"

# 4. wait again and read the result
agent-manager wait "$SID" --until idle --timeout 15m
agent-manager read "$SID"
```
