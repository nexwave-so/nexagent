# Disclaimer

## Experimental Software

Nexagent is **experimental software provided for educational and research purposes only**. It is not production-hardened, not audited, and carries no warranty of any kind — express or implied. The authors and contributors accept no liability for financial losses, missed trades, bugs, outages, or any other damages arising from its use.

**Use it at your own risk.**

---

## Financial Risk

- **Never risk more than you can afford to lose entirely.** Automated trading can and does result in total loss of deployed capital.
- Past signal performance, backtests, or paper trading results are not indicative of future live results.
- Perpetuals trading involves leverage. Even small adverse moves can exceed your margin and result in liquidation.
- The agent executes trades autonomously. Once running in live mode, it will open and close positions without further confirmation from you.
- Always start with paper trading (`PAPER_TRADING=true`) and monitor closely before switching to live mode.
- Set conservative risk limits (`MAX_POSITION_USD`, `DAILY_LOSS_LIMIT_USD`, `RISK_PER_TRADE_PCT`) before going live, and review them regularly.

---

## Key and Wallet Security

The current implementation stores private keys as plaintext environment variables. This is a pragmatic starting point for solo operators, but it has real security implications:

- Anyone with access to your `.env` file, server environment, or process list can drain your wallet.
- Never commit `.env` to version control. The `.gitignore` excludes it, but double-check before every push.
- Use a **dedicated trading wallet** funded only with the capital you intend to risk. Do not use a wallet that holds other assets.
- On cloud platforms (Render, Fly, Railway), use the platform's secret management to inject env vars — never paste keys into config files.

### Recommended improvements (not yet implemented)

The following approaches would meaningfully raise the security baseline and are worth considering before deploying with significant capital:

| Approach | What it solves |
|---|---|
| **Hardware wallet / MPC signing** (e.g. Turnkey, Privy, Fireblocks) | Private key never touches application memory in plaintext |
| **Secrets manager** (AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager) | Keys are fetched at runtime, not stored in environment; access is audited and rotatable |
| **Read-only API keys + separate signing service** | The agent process only holds a scoped credential; full signing authority lives behind a separate, hardened service |
| **Spending limits at the wallet layer** (Squads multisig, smart account policies) | Caps how much can be moved per transaction or per day regardless of whether the signing key is compromised |
| **Key rotation policy** | Regular rotation limits the blast radius of a leaked key |

None of these are a substitute for risk limits inside the agent — they are complementary layers. Defense in depth applies.

---

## No Financial Advice

Nothing in this repository constitutes financial advice, investment advice, or a recommendation to buy or sell any asset. Nexwave signals are algorithmic indicators, not personalized recommendations. Always do your own research.
