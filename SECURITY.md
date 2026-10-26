# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Report them privately through GitHub's **Security → Report a vulnerability** tab on this repository, or message me on [LinkedIn](https://www.linkedin.com/in/vrutikadani). I aim to reply within 5 business days.

## How this repo handles secrets and data

- No API keys, tokens or credentials are stored in the code. Live integrations read them from environment variables (for example `ANTHROPIC_API_KEY, HUBSPOT_TOKEN`).
- All data in this repo is synthetic or public open data. No customer, employee or company records are included.
- Dependencies are kept current by Dependabot, and every change runs the test suite in CI.
