---
name: Skill_TG_Approver
description: Send proposal JSON to Telegram, require an explicit Approve/Reject decision, and store auditable approval events locally.
---

# Skill_TG_Approver

Runnable entrypoints:

```bash
python skills/tg_approver/run.py serve --port 8787
python skills/tg_approver/run.py send --proposal-file artifacts/proposals.json
python skills/tg_approver/run.py await --proposal-file artifacts/proposals.json
```

The gate is strict:
- proposals are written to disk as `pending`
- Telegram decisions arrive through the webhook callback handler
- mock execution refuses every proposal that is not explicitly `approved`
