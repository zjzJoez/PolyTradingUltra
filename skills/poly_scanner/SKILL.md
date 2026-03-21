---
name: Skill_Poly_Scanner
description: Query Polymarket Gamma markets, apply expiry and liquidity filters, and emit normalized JSON.
---

# Skill_Poly_Scanner

Runnable entrypoint:

```bash
python skills/poly_scanner/run.py --min-liquidity 10000 --max-expiry-days 7
```

The script mirrors the scanner pattern from the referenced Mert-style market discovery flow:
- pull active markets
- focus on near-expiry opportunities
- keep only liquid markets
- emit machine-friendly JSON for downstream proposal generation
